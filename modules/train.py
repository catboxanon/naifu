import torch
import os
import lightning as pl
from omegaconf import OmegaConf
from common.utils import rank_zero_print, get_class
from common.dataset import AspectRatioDataset, worker_init_fn
from modules.sdxl_model import StableDiffusionModel
from modules.sdxl_utils import apply_snr_weight
from lightning.pytorch.utilities.model_summary import ModelSummary

def setup(fabric: pl.Fabric, config: OmegaConf) -> tuple:
    model_path = config.trainer.model_path
    model = SupervisedFineTune(
        model_path=model_path, 
        config=config, 
        device=fabric.device
    )
    dataset = AspectRatioDataset(
        batch_size=config.trainer.batch_size,
        rank=fabric.global_rank,
        dtype=torch.float32,
        base_len=config.trainer.resolution,
        **config.dataset,
    )
    dataloader = torch.utils.data.DataLoader(
        dataset,
        sampler=None,
        batch_size=None,
        persistent_workers=False,
        num_workers=config.dataset.get("num_workers", 4),
        worker_init_fn=worker_init_fn,
        shuffle=False,
        pin_memory=True,
    )
    
    params_to_optim = [{'params': model.model.parameters()}]
    if config.advanced.get("train_text_encoder_1"):
        lr = config.advanced.get("text_encoder_1_lr", config.optimizer.params.lr)
        params_to_optim.append(
            {"params": model.conditioner.embedders[0].parameters(), "lr": lr}
        )
        
    if config.advanced.get("train_text_encoder_2"):
        lr = config.advanced.get("text_encoder_2_lr", config.optimizer.params.lr)
        params_to_optim.append(
            {"params": model.conditioner.embedders[1].parameters(), "lr": lr}
        )

    optim_param = config.optimizer.params
    optimizer = get_class(config.optimizer.name)(
        params_to_optim, **optim_param
    )
    scheduler = None
    if config.get("scheduler"):
        scheduler = get_class(config.scheduler.name)(
            optimizer, **config.scheduler.params
        )
        
    model.first_stage_model.to(torch.float32)
    if fabric.is_global_zero and os.name != "nt":
        print(f"\n{ModelSummary(model, max_depth=1)}\n")
        
    model, optimizer = fabric.setup(model, optimizer)
    dataloader = fabric.setup_dataloaders(dataloader)
    return model, dataset, dataloader, optimizer, scheduler

class SupervisedFineTune(StableDiffusionModel):
    def encode_batch(self, batch):
        self.conditioner.to(self.target_device)
        return self.conditioner(batch)
    
    def forward(self, batch):
        advanced = self.config.get("advanced", {})
        if not batch["is_latent"]:
            self.first_stage_model.to(self.target_device)
            latents = self.encode_first_stage(batch["pixels"].to(self.first_stage_model.dtype))
            if torch.any(torch.isnan(latents)):
                rank_zero_print("NaN found in latents, replacing with zeros")
                latents = torch.where(torch.isnan(latents), torch.zeros_like(latents), latents)
        else:
            self.first_stage_model.cpu()
            latents = batch["pixels"]

        cond = self.encode_batch(batch)
        model_dtype = next(self.model.parameters()).dtype
        cond = {k: v.to(model_dtype) for k, v in cond.items()}

        # Sample noise that we'll add to the latents
        noise = torch.randn_like(latents, dtype=model_dtype)
        if advanced.get("offset_noise"):
            offset = torch.randn(latents.shape[0], latents.shape[1], 1, 1, device=latents.device)
            noise = torch.randn_like(latents) + float(advanced.get("offset_noise_val")) * offset

        bsz = latents.shape[0]

        # Sample a random timestep for each image
        timesteps = torch.randint(
            advanced.get("timestep_start", 0),
            advanced.get("timestep_end", 1000),
            (bsz,),
            dtype=torch.int64,
            device=latents.device,
        )

        # Add noise to the latents according to the noise magnitude at each timestep
        # (this is the forward diffusion process)
        noisy_latents = self.noise_scheduler.add_noise(latents, noise, timesteps)

        # Predict the noise residual
        noise_pred = self.model(noisy_latents, timesteps, cond)

        # Get the target for loss depending on the prediction type
        is_v = advanced.get("v_parameterization", False)
        target = noise if not is_v else self.noise_scheduler.get_velocity(latents, noise, timesteps)

        min_snr_gamma = advanced.get("min_snr", False)
        if min_snr_gamma:
            # do not mean over batch dimension for snr weight or scale v-pred loss
            loss = torch.nn.functional.mse_loss(noise_pred.float(), target.float(), reduction="none")
            loss = loss.mean([1, 2, 3])

            if min_snr_gamma:
                loss = apply_snr_weight(loss, timesteps, self.noise_scheduler, advanced.min_snr_val, is_v)
                
            loss = loss.mean()  # mean over batch dimension
        else:
            loss = torch.nn.functional.mse_loss(noise_pred.float(), target.float(), reduction="mean")

        if torch.isnan(loss).any() or torch.isinf(loss).any():
            raise FloatingPointError("Error infinite or NaN loss detected")

        return loss
