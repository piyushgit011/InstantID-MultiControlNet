# Prediction interface for Cog ⚙️
# https://github.com/replicate/cog/blob/main/docs/python.md

import os
import sys

from cog import BasePredictor, Input, Path

import cv2
import torch
import torch.nn.functional as F
import numpy as np
import random

from PIL import Image
from torchvision.transforms import Compose
from diffusers import LCMScheduler
from diffusers.utils import load_image
from diffusers.models import ControlNetModel
from diffusers.pipelines.controlnet.multicontrolnet import MultiControlNetModel
from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from transformers import CLIPImageProcessor
from insightface.app import FaceAnalysis
from controlnet_aux import OpenposeDetector

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from pipeline_stable_diffusion_xl_instantid_full import (
    StableDiffusionXLInstantIDPipeline,
    draw_kps,
)
from depth_anything.dpt import DepthAnything
from depth_anything.util.transform import Resize, NormalizeImage, PrepareForNet

# for ip-adapter, ControlNetModel
CHECKPOINTS_CACHE = "./checkpoints"
POSE_CHKPT_CACHE = f"{CHECKPOINTS_CACHE}/pose"
CANNY_CHKPT_CACHE = f"{CHECKPOINTS_CACHE}/canny"
DEPTH_CHKPT_CACHE = f"{CHECKPOINTS_CACHE}/depth"

# for SDXL model
SD_MODEL_CACHE = "./sd_model"
SD_MODEL_NAME = "GraydientPlatformAPI/albedobase2-xl"

# safety checker model
SAFETY_MODEL_CACHE = "./safety_cache"
FEATURE_EXTRACT_CACHE = "feature_extractor"

# global variable
MAX_SEED = np.iinfo(np.int32).max
device = "cuda" if torch.cuda.is_available() else "cpu"
dtype = torch.float16 if str(device).__contains__("cuda") else torch.float32
enable_lcm_arg = False


def resize_img(
    input_image,
    max_side=1280,
    min_side=1024,
    size=None,
    pad_to_max_side=False,
    mode=Image.BILINEAR,
    base_pixel_number=64,
):
    """Resize input image"""
    w, h = input_image.size
    if size is not None:
        w_resize_new, h_resize_new = size
    else:
        ratio = min_side / min(h, w)
        w, h = round(ratio * w), round(ratio * h)
        ratio = max_side / max(h, w)
        input_image = input_image.resize([round(ratio * w), round(ratio * h)], mode)
        w_resize_new = (round(ratio * w) // base_pixel_number) * base_pixel_number
        h_resize_new = (round(ratio * h) // base_pixel_number) * base_pixel_number
    input_image = input_image.resize([w_resize_new, h_resize_new], mode)

    if pad_to_max_side:
        res = np.ones([max_side, max_side, 3], dtype=np.uint8) * 255
        offset_x = (max_side - w_resize_new) // 2
        offset_y = (max_side - h_resize_new) // 2
        res[
            offset_y : offset_y + h_resize_new, offset_x : offset_x + w_resize_new
        ] = np.array(input_image)
        input_image = Image.fromarray(res)
    return input_image

def convert_from_cv2_to_image(img: np.ndarray) -> Image:
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))

def convert_from_image_to_cv2(img: Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


class Predictor(BasePredictor):
    def setup(self) -> None:
        """Load safety checker"""
        self.safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            SAFETY_MODEL_CACHE, torch_dtype=dtype
        ).to(device)
        self.feature_extractor = CLIPImageProcessor.from_pretrained(FEATURE_EXTRACT_CACHE)

        """Load the model into memory to make running multiple predictions efficient"""
        self.width, self.height = 640, 640
        self.app = FaceAnalysis(
            name="antelopev2",
            root="./",
            providers=["CPUExecutionProvider"],
        )
        self.app.prepare(ctx_id=0, det_size=(self.width, self.height))

        # Load openpose and depth-anything controlnet pipelines
        self.openpose = OpenposeDetector.from_pretrained("lllyasviel/ControlNet")
        self.depth_anything = DepthAnything.from_pretrained('LiheYoung/depth_anything_vitl14').to(device).eval()

        self.transform = Compose([
            Resize(
                width=518,
                height=518,
                resize_target=False,
                keep_aspect_ratio=True,
                ensure_multiple_of=14,
                resize_method='lower_bound',
                image_interpolation_method=cv2.INTER_CUBIC,
            ),
            NormalizeImage(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            PrepareForNet(),
        ])

        # Path to InstantID models
        face_adapter = f"{CHECKPOINTS_CACHE}/ip-adapter.bin"
        controlnet_path = f"{CHECKPOINTS_CACHE}/ControlNetModel"

        # Load pipeline face ControlNetModel
        self.controlnet_identitynet = ControlNetModel.from_pretrained(
            controlnet_path,
            torch_dtype=dtype,
            cache_dir=CHECKPOINTS_CACHE,
            use_safetensors=True,
            local_files_only=True,
        )

        # Load controlnet-pose/canny/depth
        controlnet_pose_model = "thibaud/controlnet-openpose-sdxl-1.0"
        controlnet_canny_model = "diffusers/controlnet-canny-sdxl-1.0"
        controlnet_depth_model = "diffusers/controlnet-depth-sdxl-1.0-small"

        self.controlnet_pose = ControlNetModel.from_pretrained(
            controlnet_pose_model,
            torch_dtype=dtype,
            use_safetensors=True,
            cache_dir=POSE_CHKPT_CACHE,
            local_files_only=True,
        ).to(device)
        self.controlnet_canny = ControlNetModel.from_pretrained(
            controlnet_canny_model,
            torch_dtype=dtype,
            use_safetensors=True,
            cache_dir=CANNY_CHKPT_CACHE,
            local_files_only=True,
        ).to(device)
        self.controlnet_depth = ControlNetModel.from_pretrained(
            controlnet_depth_model,
            torch_dtype=dtype,
            use_safetensors=True,
            cache_dir=DEPTH_CHKPT_CACHE,
            local_files_only=True,    
        ).to(device)

        self.pipe = StableDiffusionXLInstantIDPipeline.from_pretrained(
            SD_MODEL_NAME,
            controlnet=[self.controlnet_identitynet],
            torch_dtype=dtype,
            cache_dir=SD_MODEL_CACHE,
            use_safetensors=True,
        ).to(device)

        # load LCM LoRA
        self.pipe.load_lora_weights(f"{CHECKPOINTS_CACHE}/pytorch_lora_weights.safetensors")
        self.pipe.fuse_lora()
        self.pipe.scheduler = LCMScheduler.from_config(self.pipe.scheduler.config)

        self.pipe.cuda()
        self.pipe.load_ip_adapter_instantid(face_adapter)
        self.pipe.image_proj_model.to("cuda")
        self.pipe.unet.to("cuda")

    def get_depth_map(self, image):
        """Get the depth map from input image"""
        image = np.array(image) / 255.0
        h, w = image.shape[:2]

        image = self.transform({'image': image})['image']
        image = torch.from_numpy(image).unsqueeze(0).to("cuda")

        with torch.no_grad():
            depth = self.depth_anything(image)

        depth = F.interpolate(depth[None], (h, w), mode='bilinear', align_corners=False)[0, 0]
        depth = (depth - depth.min()) / (depth.max() - depth.min()) * 255.0
        depth = depth.cpu().numpy().astype(np.uint8)
        depth_image = Image.fromarray(depth)
        return depth_image

    def get_canny_image(self, image, t1=100, t2=200):
        """Get the canny edges from input image"""
        image = convert_from_image_to_cv2(image)
        edges = cv2.Canny(image, t1, t2)
        return Image.fromarray(edges, "L")

    def run_safety_checker(self, image) -> (list, list):
        """Detect nsfw content"""
        safety_checker_input = self.feature_extractor(image, return_tensors="pt").to(device)
        np_image = [np.array(val) for val in image]
        image, has_nsfw_concept = self.safety_checker(
            images=np_image,
            clip_input=safety_checker_input.pixel_values.to(dtype),
        )
        return image, has_nsfw_concept

    @torch.inference_mode()
    def predict(
        self,
        face_image_path: Path = Input(description="Image of your face"),
        pose_image_path: Path = Input(
            description="Reference pose image",
            default=None,
        ),
        prompt: str = Input(
            description="Input prompt",
            default="a person",
        ),
        negative_prompt: str = Input(
            description="Input negative prompt",
            default="ugly, low quality, deformed face",
        ),
        width: int = Input(
            description="Width of output image",
            default=640,
            ge=512,
            le=2048,
        ),
        height: int = Input(
            description="Height of output image",
            default=640,
            ge=512,
            le=2048,
        ),
        adapter_strength_ratio: float = Input(
            description="Image adapter strength (for detail)",
            default=0.8,
            ge=0,
            le=1,
        ),
        identitynet_strength_ratio: float = Input(
            description="IdentityNet strength (for fidelity)",
            default=0.8,
            ge=0,
            le=1,
        ),
        pose: bool = Input(
            description="Use pose for skeleton inference",
            default=False,
        ),
        canny: bool = Input(
            description="Use canny for edge detection",
            default=False,
        ),
        depth_map: bool = Input(
            description="Use depth for depth map estimation",
            default=False,
        ),
        pose_strength: float = Input(
            default=0.5,
            ge=0,
            le=1.5,
        ),
        canny_strength: float = Input(
            default=0.5,
            ge=0,
            le=1.5,
        ),
        depth_strength: float = Input(
            default=0.5,
            ge=0,
            le=1.5,
        ),
        num_steps: int = Input(
            description="Number of denoising steps. With LCM-LoRA, optimum is 6-8.",
            default=6,
            ge=1,
            le=30,
        ),
        guidance_scale: float = Input(
            description="Scale for classifier-free guidance. With LCM-LoRA, optimum is 0-5.",
            default=0,
            ge=0,
            le=10,
        ),
        seed: int = Input(
            description="Seed number. Set to non-zero to make the image reproducible.",
            default=None,
            ge=1,
            le=MAX_SEED,
        ),
        safety_checker: bool = Input(
            description="Safety checker is enabled by default. Un-tick to expose unfiltered results.",
            default=True,
        ),
    ) -> Path:
        """Run a single prediction on the model"""
        # Resize the output if the provided dimensions are different from the current ones
        if self.width != width or self.height != height:
            print(f"[!] Resizing output to {width}x{height}")
            self.width = width
            self.height = height
            self.app.prepare(ctx_id=0, det_size=(self.width, self.height))

        # Load and resize the face image
        face_image = load_image(str(face_image_path))
        face_image = resize_img(face_image, max_side=1024)
        face_image_cv2 = convert_from_image_to_cv2(face_image)
        height, width, _ = face_image_cv2.shape

        # Extract face features
        face_info = self.app.get(face_image_cv2)
        if len(face_info) == 0:
            raise ValueError(
                "Unable to detect your face in the photo. Please upload a different photo with a clear face."
            )
        face_info = sorted(
            face_info,
            key=lambda x: (x["bbox"][2] - x["bbox"][0]) * x["bbox"][3] - x["bbox"][1],
        )[-1]  # only use the maximum face
        face_emb = face_info["embedding"]
        face_kps = draw_kps(convert_from_cv2_to_image(face_image_cv2), face_info["kps"])
        img_controlnet = face_image

        # If pose image is provided, use it to extra the pose
        if pose_image_path is not None:
            pose_image = load_image(str(pose_image_path))
            pose_image = resize_img(pose_image, max_side=1024)
            img_controlnet = pose_image
            pose_image_cv2 = convert_from_image_to_cv2(pose_image)

            # Extract face features from the reference pose image
            face_info = self.app.get(pose_image_cv2)
            if len(face_info) == 0:
                raise ValueError(
                    "Unable to detect a face in the reference image. Please upload another person's image."
                )
            face_info = face_info[-1]
            face_kps = draw_kps(pose_image, face_info["kps"])
            width, height = face_kps.size

        controlnet_map = {
            "pose": self.controlnet_pose,
            "canny": self.controlnet_canny,
            "depth": self.controlnet_depth,
        }
        controlnet_map_fn = {
            "pose": self.openpose,
            "canny": self.get_canny_image,
            "depth": self.get_depth_map,
        }

        controlnet_selection = []
        if pose:
            controlnet_selection.append("pose")
        if canny:
            controlnet_selection.append("canny")
        if depth_map:
            controlnet_selection.append("depth")

        if len(controlnet_selection) > 0:
            controlnet_scales = {
                "pose": pose_strength,
                "canny": canny_strength,
                "depth": depth_strength,
            }
            self.pipe.controlnet = MultiControlNetModel([self.controlnet_identitynet] + [controlnet_map[s] for s in controlnet_selection])
            control_scales = [float(identitynet_strength_ratio)] + [controlnet_scales[s] for s in controlnet_selection]
            control_images = [face_kps] + [
                controlnet_map_fn[s](img_controlnet).resize((width, height))
                for s in controlnet_selection
            ]
        else:
            self.pipe.controlnet = self.controlnet_identitynet
            control_scales = float(identitynet_strength_ratio)
            control_images = face_kps

        if seed == 0:
            seed = random.randint(1, MAX_SEED)
        generator = torch.Generator(device=device).manual_seed(seed)

        self.pipe.set_ip_adapter_scale(adapter_strength_ratio)
        image = self.pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            image_embeds=face_emb,
            image=control_images,
            control_mask=None,
            controlnet_conditioning_scale=control_scales,
            num_inference_steps=num_steps,
            guidance_scale=guidance_scale,
            generator=generator,
            height=height,
            width=width,
        ).images[0]
        output_path = "result.jpg"

        output = [image]
        if safety_checker:
            image_list, has_nsfw_content = self.run_safety_checker(output)
            if has_nsfw_content[0]:
                print("NSFW content detected. Try running it again, rephrase different prompt or add 'nsfw' in the negative prompt.")
                black = Image.fromarray(np.uint8(image_list[0])).convert('RGB')    # black box image
                black.save(output_path)
            else:
                image.save(output_path)
        else:
            image.save(output_path)

        return Path(output_path)
