"""data/model/<name>.onnx + data/model/<name>.yaml (preprocessing and calibration settings).

    input:
      size: 224              # square input
      crop: 0.875            # center crop ratio of the short side
      interpolation: bilinear  # bilinear | bicubic
      mean: [0.485, 0.456, 0.406]  # RGB, on [0, 1]
      std:  [0.229, 0.224, 0.225]
    calibration_images: 300  # used only for float32 ONNX
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO_ROOT / "data" / "model"
BUILD_DIR = REPO_ROOT / "build"


@dataclass(frozen=True)
class ModelConfig:
    name: str
    onnx: Path
    size: int
    crop: float
    interpolation: str
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    calibration_images: int


def model_config(name: str) -> ModelConfig:
    onnx = MODEL_DIR / f"{name}.onnx"
    cfg_path = MODEL_DIR / f"{name}.yaml"
    for p in (onnx, cfg_path):
        if not p.exists():
            raise FileNotFoundError(f"{p} not found")
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    inp = cfg.get("input", {})
    interp = inp.get("interpolation", "bilinear")
    if interp not in ("bilinear", "bicubic"):
        raise ValueError(f"{cfg_path}: interpolation must be bilinear or bicubic")
    return ModelConfig(
        name=name, onnx=onnx,
        size=int(inp.get("size", 224)), crop=float(inp.get("crop", 0.875)), interpolation=interp,
        mean=tuple(float(v) for v in inp.get("mean", (0.485, 0.456, 0.406))),
        std=tuple(float(v) for v in inp.get("std", (0.229, 0.224, 0.225))),
        calibration_images=int(cfg.get("calibration_images", 300)),
    )


def model_build_dir(name: str) -> Path:
    return BUILD_DIR / name
