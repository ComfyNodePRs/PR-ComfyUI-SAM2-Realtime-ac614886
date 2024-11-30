import torch
import os
import requests
import numpy as np
import logging
import json
import ast

import sys

import cv2

# Add the directory containing 'sam2_realtime' to sys.path
current_directory = os.path.dirname(os.path.abspath(__file__))
sam2_realtime_path = os.path.join(current_directory)  # Adjust the relative path
sys.path.append(sam2_realtime_path)

from sam2_realtime.sam2_tensor_predictor import SAM2TensorPredictor
from comfy.utils import load_torch_file

from omegaconf import OmegaConf
from hydra.utils import instantiate
from hydra import initialize_config_dir, compose
from hydra.core.global_hydra import GlobalHydra

import comfy.model_management as mm
import folder_paths

script_directory = os.path.dirname(os.path.abspath(__file__))

class DownloadAndLoadSAM2RealtimeModel:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "model": ([ 
                    'sam2_hiera_tiny.pt',
                    ],),
            "segmentor": (
                    ['realtime'],
                    ),
            "device": (['cuda', 'cpu', 'mps'], ),
            "precision": ([ 'fp16','bf16','fp32'],
                    {
                    "default": 'fp16'
                    }),

            },
        }

    RETURN_TYPES = ("SAM2MODEL",)
    RETURN_NAMES = ("sam2_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "SAM2-Realtime"

    def loadmodel(self, model, segmentor, device, precision):
        if precision != 'fp32' and device == 'cpu':
            raise ValueError("fp16 and bf16 are not supported on cpu")

        if device == "cuda":
            if torch.cuda.get_device_properties(0).major >= 8:
                # turn on tfloat32 for Ampere GPUs (https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices)
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]
        device = {"cuda": torch.device("cuda"), "cpu": torch.device("cpu"), "mps": torch.device("mps")}[device]

        download_path = os.path.join(folder_paths.models_dir, "sam2")
        model_path = os.path.join(download_path, model)
        print("model_path: ", model_path)

        url = "https://dl.fbaipublicfiles.com/segment_anything_2/072824/sam2_hiera_tiny.pt"

        if not os.path.exists(model_path):
            print(f"Downloading SAM2 model to: {model_path}")
            response = requests.get(url, stream=True)
            response.raise_for_status()

            with open(model_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            print(f"Model saved to {model_path}")

        config_dir = os.path.join(script_directory, "sam2_configs") 

        # Code ripped out of sam2.build_sam.build_sam2_camera_predictor to appease Hydra
        model_cfg = "sam2_hiera_t.yaml" #TODO(pschroedl): remove hardcoded config and path
        with initialize_config_dir(config_dir=config_dir, version_base=None):
            cfg = compose(config_name=model_cfg)

            hydra_overrides = [
                "++model._target_=sam2_realtime.sam2_tensor_predictor.SAM2TensorPredictor",
            ]
            hydra_overrides_extra = [
                "++model.sam_mask_decoder_extra_args.dynamic_multimask_via_stability=true",
                "++model.sam_mask_decoder_extra_args.dynamic_multimask_stability_delta=0.05",
                "++model.sam_mask_decoder_extra_args.dynamic_multimask_stability_thresh=0.98",
                "++model.binarize_mask_from_pts_for_mem_enc=true",
                "++model.fill_hole_area=8",
            ]
            hydra_overrides.extend(hydra_overrides_extra)

            cfg = compose(config_name=model_cfg, overrides=hydra_overrides)
            OmegaConf.resolve(cfg)

            model = instantiate(cfg.model, _recursive_=True)
        
        def _load_checkpoint(model, ckpt_path):
            if ckpt_path is not None:
                sd = torch.load(ckpt_path, map_location="cpu")["model"]
                missing_keys, unexpected_keys = model.load_state_dict(sd)
                if missing_keys:
                    logging.error(missing_keys)
                    raise RuntimeError()
                if unexpected_keys:
                    logging.error(unexpected_keys)
                    raise RuntimeError()
                logging.info("Loaded checkpoint sucessfully")

        _load_checkpoint(model, model_path)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = model.to(device)
        model.eval()
        
        sam2_model = {
            'model': model, 
            'dtype': dtype,
            'device': device,
            'segmentor' : segmentor,
            'version': "2.0"
            }

        return (sam2_model,)

class Sam2RealtimeSegmentation:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "sam2_model": ("SAM2MODEL",),
                # "keep_model_loaded": ("BOOLEAN", {"default": True}),
            },
           "optional": {
                "coordinates_positive": ("STRING", {"forceInput": True}),
                "point_labels": ("STRING", {"forceInput": True}),
                # "coordinates_negative": ("STRING", {"forceInput": True}),
                # "bboxes": ("BBOX", ),
                # "individual_objects": ("BOOLEAN", {"default": False}),
                # "mask": ("MASK", ),
                "threshold": ("FLOAT", {"forceInput": True}),
                "show_point": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_NAMES = ("PROCESSED_IMAGES","MASK",)
    RETURN_TYPES = ("IMAGE", "IMAGE",)
    FUNCTION = "segment_images"
    CATEGORY = "SAM2-Realtime"

    def __init__(self):
        self.predictor = None
        self.if_init = False


    def _process_mask(self, mask: np.ndarray, frame_shape: tuple) -> np.ndarray:
        if mask.shape[0] == 0:
            logging.warning("Empty mask received")
            return np.zeros((frame_shape[0], frame_shape[1]), dtype="uint8")
        
        colors = [
            [255, 0, 255],  # Purple
            [0, 255, 255],  # Yellow
            [255, 255, 0],  # Cyan
            [0, 255, 0],    # Green
            [255, 0, 0],    # Blue
        ]
        
        combined_colored_mask = np.zeros((frame_shape[0], frame_shape[1], 4), dtype="uint8")
        
        for i in range(mask.shape[0]):
            current_mask = (mask[i, 0] > 0).cpu().numpy().astype("uint8") * 255
            if current_mask.shape[:2] != frame_shape[:2]:
                current_mask = cv2.resize(current_mask, (frame_shape[1], frame_shape[0]))
            
            # Create BGRA mask with transparency
            colored_mask = np.zeros((frame_shape[0], frame_shape[1], 4), dtype="uint8")
            color = colors[i % len(colors)]
            colored_mask[current_mask > 0] = color + [128]  # Add alpha value of 128
            
            # Alpha blend with existing masks
            alpha = colored_mask[:, :, 3:4] / 255.0
            combined_colored_mask = (1 - alpha) * combined_colored_mask + alpha * colored_mask

        # Convert back to BGR for display
        combined_colored_mask = combined_colored_mask[:, :, :3].astype("uint8")
        return combined_colored_mask

    def segment_images(
        self,
        images,
        sam2_model,
        # keep_model_loaded,
        coordinates_positive=None,
        # coordinates_negative=None,
        point_labels=None,
        # bboxes=None,
        # individual_objects=False,
        # mask=None,
        threshold=0.5,
        show_point=False,
    ):
        model = sam2_model["model"]
        device = sam2_model["device"]

        device = torch.device("cuda")
        model.to(device)

        processed_frames = []
        mask_list = []
        # The `model` is equivalent to `predictor` returned by sam2.build_sam.build_sam2_camera_predictor
        if self.predictor is None:
            self.predictor = model   

        def process_frame(frame, frame_idx):
            with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16):
                frame = frame.to(device).float()

                if not self.if_init:
                    self.predictor.load_first_frame(frame)
                    self.if_init = True

                    coordinates_positive_list = ast.literal_eval(coordinates_positive)
                    point_labels_list = ast.literal_eval(point_labels)
                    point_labels_list = list(map(int, point_labels_list))

                    for idx, point in enumerate(coordinates_positive_list):
                        point_tuple = tuple(map(int, point))
                        _, _, out_mask_logits = self.predictor.add_new_prompt(
                            frame_idx=0, 
                            obj_id=idx + 1, 
                            points=[point_tuple], 
                            labels=[point_labels_list[idx]]
                        )
                else:
                    out_obj_ids, out_mask_logits = self.predictor.track(frame)

                if out_mask_logits.shape[0] > 0:
                    mask = (out_mask_logits[0, 0] > threshold).byte()
                    mask = torch.nn.functional.interpolate(
                        mask.unsqueeze(0).unsqueeze(0).float(), 
                        size=(frame.shape[0], frame.shape[1]), 
                        mode='nearest'
                    ).squeeze(0).squeeze(0).byte()  # Move the interpolated mask to the correct device
                else:
                    mask = torch.ones((frame.shape[0], frame.shape[1]), device=device, dtype=torch.uint8)

                automask_colored = self._process_mask(mask,frame.shape)

                # Draw points on the mask
                if show_point:
                    for point in coordinates_positive:
                        cv2.circle(automask_colored, tuple(point), radius=5, color=(0, 0, 255), thickness=-1)

                automasked_frame = torch.add(frame * 0.7, automask_colored * 0.3)
                processed_frames.append(automasked_frame)

                # TODO: This "mask" should be 1 channel to be returned as MASK type
                constructed_mask = torch.add(frame * 0.1, mask * 0.9)
                mask_list.append(constructed_mask)


        for frame_idx, img in enumerate(images):
            process_frame(img, frame_idx)

        stacked_masks = torch.stack(mask_list, dim=0)
        stacked_frames = torch.stack(processed_frames, dim=0) 
        return (stacked_frames, stacked_masks)

NODE_CLASS_MAPPINGS = {
    "DownloadAndLoadSAM2RealtimeModel": DownloadAndLoadSAM2RealtimeModel,
    "Sam2RealtimeSegmentation": Sam2RealtimeSegmentation
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DownloadAndLoadSAM2RealtimeModel": "(Down)Load sam2_realtime Model",
    "Sam2RealtimeSegmentation": "Sam2RealtimeSegmentation"
}
