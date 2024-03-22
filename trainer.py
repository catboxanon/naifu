# python trainer.py --config config/test.yaml
import os
import sys

# Hide welcome message from bitsandbytes
os.environ.update({"BITSANDBYTES_NOWELCOME": "1"})

import torch
import lightning as pl
import argparse

from common.utils import get_class, setup_smddp
from common.trainer import Trainer
from omegaconf import OmegaConf
from lightning.fabric.connector import _is_using_cli

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str)
    parser.add_argument("--resume", action="store_true")
    first_args = sys.argv[1]
    
    if first_args.startswith("--"):
        args = parser.parse_args()
    else:
        args = parser.parse_args(sys.argv[2:])
        args.config = first_args

    config = OmegaConf.load(args.config)
    config.trainer.resume = args.resume
    plugins = []

    strategy = config.lightning.pop("strategy", "auto")
    if "." in strategy:
        _params = config.lightning.pop("strategy_params", {})
        strategy = get_class(strategy)(**_params)

    if os.environ.get("SM_TRAINING", False) or os.environ.get("SM_HOSTS", False):
        strategy, config = setup_smddp(config)

    loggers = pl.fabric.loggers.CSVLogger(".")
    if config.trainer.wandb_id != "":
        from lightning.pytorch.loggers import WandbLogger
        loggers = WandbLogger(project=config.trainer.wandb_id)

    fabric = pl.Fabric(
        loggers=[loggers], 
        plugins=plugins, 
        strategy=strategy, 
        **config.lightning
    )
    if not _is_using_cli():
        fabric.launch()
        
    fabric.barrier()
    fabric.seed_everything(config.trainer.seed + fabric.global_rank)
    
    Trainer(fabric, config).train_loop()


if __name__ == "__main__":
    main()
