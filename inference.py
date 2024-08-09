from dataclasses import dataclass
import os
import re
import time
from glob import iglob
from io import BytesIO

import streamlit as st
import torch
from einops import rearrange
from fire import Fire
from PIL import Image
from st_keyup import st_keyup
from torchvision import transforms

from common.model_utils import load_models, configs
from common.model_utils import denoise, get_noise, get_schedule, prepare, unpack

# pip install fire streamlit-keyup


@dataclass
class SamplingOptions:
    prompt: str
    width: int
    height: int
    num_steps: int
    guidance: float
    seed: int | None

@st.cache_resource()
def get_models(name: str, ckpt_path: str, ae_path: str, device: torch.device):
    return load_models(name, ckpt_path, ae_path, device)


def get_image() -> torch.Tensor | None:
    image = st.file_uploader("Input", type=["jpg", "JPEG", "png"])
    if image is None:
        return None
    image = Image.open(image).convert("RGB")

    transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Lambda(lambda x: 2.0 * x - 1.0),
        ]
    )
    img: torch.Tensor = transform(image)
    return img[None, ...]


@torch.inference_mode()
def main(
    device: str = "cuda" if torch.cuda.is_available() else "cpu",
    output_dir: str = "output",
):
    torch_device = torch.device(device)
    names = list(configs.keys())
    name = st.selectbox("Which model to load?", names)
    if name is None or not st.checkbox("Load model", False):
        return

    is_schnell = name == "flux-schnell"
    ckpt_path = "/storage/dev/nyanko/flux-dev/flux1-dev.sft"
    ae_path = "/storage/dev/nyanko/flux-dev/ae.sft"
    if is_schnell:
        ckpt_path = "/storage/dev/nyanko/flux-dev/flux1-schnell.sft"
    
    model, ae, t5, clip = get_models(
        name,
        ckpt_path=ckpt_path,
        ae_path=ae_path,
        device=torch_device,
    )

    model = model.eval()
    do_img2img = (
        st.checkbox(
            "Image to Image",
            False,
            disabled=is_schnell,
            help="Partially noise an image and denoise again to get variations.\n\nOnly works for flux-dev",
        )
        and not is_schnell
    )
    if do_img2img:
        init_image = get_image()
        if init_image is None:
            st.warning("Please add an image to do image to image")
        image2image_strength = st.number_input("Noising strength", min_value=0.0, max_value=1.0, value=0.8)
        if init_image is not None:
            h, w = init_image.shape[-2:]
            st.write(f"Got image of size {w}x{h} ({h*w/1e6:.2f}MP)")
        resize_img = st.checkbox("Resize image", False) or init_image is None
    else:
        init_image = None
        resize_img = True
        image2image_strength = 0.0

    # allow for packing and conversion to latent space
    width = int(
        16 * (st.number_input("Width", min_value=128, value=1360, step=16, disabled=not resize_img) // 16)
    )
    height = int(
        16 * (st.number_input("Height", min_value=128, value=768, step=16, disabled=not resize_img) // 16)
    )
    num_steps = int(st.number_input("Number of steps", min_value=1, value=(4 if is_schnell else 50)))
    guidance = float(st.number_input("Guidance", min_value=1.0, value=3.5, disabled=is_schnell))
    seed_str = st.text_input("Seed", disabled=is_schnell)
    if seed_str.isdecimal():
        seed = int(seed_str)
    else:
        st.info("No seed set, set to positive integer to enable")
        seed = None
    save_samples = st.checkbox("Save samples?", not is_schnell)

    default_prompt = (
        "a photo of a forest with mist swirling around the tree trunks. The word "
        '"FLUX" is painted over it in big, red brush strokes with visible texture'
    )
    prompt = st_keyup("Enter a prompt", value=default_prompt, debounce=300, key="interactive_text")

    output_name = os.path.join(output_dir, "img_{idx}.jpg")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        idx = 0
    else:
        fns = [fn for fn in iglob(output_name.format(idx="*")) if re.search(r"img_[0-9]\.jpg$", fn)]
        if len(fns) > 0:
            idx = max(int(fn.split("_")[-1].split(".")[0]) for fn in fns) + 1
        else:
            idx = 0

    rng = torch.Generator(device="cpu")

    if "seed" not in st.session_state:
        st.session_state.seed = rng.seed()

    def increment_counter():
        st.session_state.seed += 1

    def decrement_counter():
        if st.session_state.seed > 0:
            st.session_state.seed -= 1

    opts = SamplingOptions(
        prompt=prompt,
        width=width,
        height=height,
        num_steps=num_steps,
        guidance=guidance,
        seed=seed,
    )

    if name == "flux-schnell":
        cols = st.columns([5, 1, 1, 5])
        with cols[1]:
            st.button("↩", on_click=increment_counter)
        with cols[2]:
            st.button("↪", on_click=decrement_counter)
            
    if is_schnell or st.button("Sample"):
        if is_schnell:
            opts.seed = st.session_state.seed
        elif opts.seed is None:
            opts.seed = rng.seed()
        print(f"Generating '{opts.prompt}' with seed {opts.seed}")
        t0 = time.perf_counter()

        if init_image is not None:
            if resize_img:
                init_image = torch.nn.functional.interpolate(init_image, (opts.height, opts.width))
            else:
                h, w = init_image.shape[-2:]
                init_image = init_image[..., : 16 * (h // 16), : 16 * (w // 16)]
                opts.height = init_image.shape[-2]
                opts.width = init_image.shape[-1]
                
            init_image = ae.encode(init_image.to(torch_device))

        # prepare input
        x = get_noise(
            1,
            opts.height,
            opts.width,
            device=torch_device,
            dtype=torch.bfloat16,
            seed=opts.seed,
        )
        # divide pixel space by 16**2 to acocunt for latent space conversion
        timesteps = get_schedule(
            opts.num_steps,
            (x.shape[-1] * x.shape[-2]) // 4,
            shift=(not is_schnell),
        )
        if init_image is not None:
            t_idx = int((1 - image2image_strength) * num_steps)
            t = timesteps[t_idx]
            timesteps = timesteps[t_idx:]
            x = t * x + (1.0 - t) * init_image.to(x.dtype)

        inp = prepare(t5=t5, clip=clip, img=x, prompt=opts.prompt)

        # denoise initial noise
        x = denoise(model, **inp, timesteps=timesteps, guidance=opts.guidance)

        # decode latents to pixel space
        x = unpack(x.float(), opts.height, opts.width)
        with torch.autocast(device_type=torch_device.type, dtype=torch.bfloat16):
            x = ae.decode(x)

        t1 = time.perf_counter()
        fn = output_name.format(idx=idx)
        print(f"Done in {t1 - t0:.1f}s.")
        # bring into PIL format and save
        x = x.clamp(-1, 1)
        x = rearrange(x[0], "c h w -> h w c")
        img = Image.fromarray((127.5 * (x + 1.0)).cpu().byte().numpy())
        # buffer = BytesIO()
        # exif_data = Image.Exif()
        # if init_image is None:
        #     exif_data[ExifTags.Base.Software] = "AI generated;txt2img;flux"
        # else:
        #     exif_data[ExifTags.Base.Software] = "AI generated;img2img;flux"
        # exif_data[ExifTags.Base.Make] = "Black Forest Labs"
        # exif_data[ExifTags.Base.Model] = name
        # if add_sampling_metadata:
        #     exif_data[ExifTags.Base.ImageDescription] = prompt
        # img.save(buffer, format="jpeg", exif=exif_data, quality=95, subsampling=0)
        buffer = BytesIO()
        img.save(buffer, format="webp", quality=95)
        img_bytes = buffer.getvalue()
        if save_samples:
            print(f"Saving {fn}")
            with open(fn, "wb") as file:
                file.write(img_bytes)
            idx += 1

        st.session_state["samples"] = {
            "prompt": opts.prompt,
            "img": img,
            "seed": opts.seed,
            "bytes": img_bytes,
        }
        opts.seed = None
        # else:
        #     st.warning("Your generated image may contain NSFW content.")
        #     st.session_state["samples"] = None

    samples = st.session_state.get("samples", None)
    if samples is not None:
        st.image(samples["img"], caption=samples["prompt"])
        st.download_button(
            "Download full-resolution",
            samples["bytes"],
            file_name="generated.jpg",
            mime="image/jpg",
        )
        st.write(f"Seed: {samples['seed']}")


def app():
    Fire(main)


if __name__ == "__main__":
    app()