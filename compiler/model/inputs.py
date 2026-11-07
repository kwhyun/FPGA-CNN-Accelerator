"""RGB uint8 → model input (ONNX float input / accelerator first-layer int8)."""
from __future__ import annotations

import numpy as np

from compiler.model.config import ModelConfig, model_config
from compiler.model.images import load_rgb


def load_image(cfg: ModelConfig, index: int) -> np.ndarray:
    return load_rgb(index, cfg.size, cfg.crop, cfg.interpolation)


def normalize(cfg: ModelConfig, rgb: np.ndarray) -> np.ndarray:
    """uint8 [H, W, 3] → float32 [H, W, 3], (x/255 − mean)/std."""
    mean = np.asarray(cfg.mean, np.float32)
    std = np.asarray(cfg.std, np.float32)
    return ((rgb.astype(np.float32) / np.float32(255.0)) - mean) / std


def onnx_input(cfg: ModelConfig, rgb: np.ndarray) -> np.ndarray:
    """ONNX model input float32 [1, 3, H, W]."""
    return np.ascontiguousarray(normalize(cfg, rgb).transpose(2, 0, 1)[None])


def quantize_input(x: np.ndarray, scale: float, zp: int) -> np.ndarray:
    """Same rule as ONNX QuantizeLinear: float32 x/scale, half-to-even, int8 saturate."""
    q = np.rint(x.astype(np.float32) / np.float32(scale)).astype(np.int32) + zp
    return np.clip(q, -128, 127).astype(np.int8)


def first_layer_input(model, rgb: np.ndarray) -> np.ndarray:
    """Accelerator first-layer int8 input [H, W, C]."""
    return quantize_input(normalize(model_config(model.name), rgb), model.input_scale, model.input_zp)
