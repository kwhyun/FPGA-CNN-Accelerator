"""ImageNet validation images → uint8 RGB arrays and ground-truth labels.

Center crop on the short side → size×size resize (crop, interpolation from the model yaml).
Per-model normalization/quantization is handled by :mod:`compiler.model.inputs`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import scipy.io as sio
from PIL import Image

from compiler.model.config import REPO_ROOT

VAL_ROOT = REPO_ROOT / "data" / "dataset"
IMG_DIR = VAL_ROOT / "ILSVRC2012_img_val"
META_MAT = VAL_ROOT / "ILSVRC2012_devkit_t12" / "data" / "meta.mat"
GROUND_TRUTH = VAL_ROOT / "ILSVRC2012_devkit_t12" / "data" / "ILSVRC2012_validation_ground_truth.txt"

INPUT_SIZE = 224
CROP_FRACTION = 0.875
_RESAMPLE = {"bilinear": Image.BILINEAR, "bicubic": Image.BICUBIC}


def image_path(index: int) -> Path:
    """1-based ImageNet validation index → JPEG path."""
    return IMG_DIR / f"ILSVRC2012_val_{index:08d}.JPEG"


def load_rgb(index: int, size: int = INPUT_SIZE, crop: float = CROP_FRACTION,
             interpolation: str = "bilinear") -> np.ndarray:
    """Return uint8 ``[size, size, 3]`` RGB after center crop and bilinear resize."""
    img = Image.open(image_path(index)).convert("RGB")
    w, h = img.size
    side = min(w, h) * crop
    left = (w - side) / 2
    top = (h - side) / 2
    img = img.resize((size, size), _RESAMPLE[interpolation], box=(left, top, left + side, top + side))
    return np.asarray(img, dtype=np.uint8)


@lru_cache(maxsize=1)
def _id_to_class() -> np.ndarray:
    """ILSVRC2012_ID (1..1000) → class in WNID alphabetical order (0..999)."""
    synsets = sio.loadmat(str(META_MAT))["synsets"]
    wnid_by_id: dict[int, str] = {}
    for i in range(synsets.shape[0]):
        row = synsets[i, 0]
        sid = int(row["ILSVRC2012_ID"][0, 0])
        if 1 <= sid <= 1000:
            wnid_by_id[sid] = str(row["WNID"][0])
    order = sorted(wnid_by_id.values())
    rank = {w: k for k, w in enumerate(order)}
    table = np.zeros(1001, dtype=np.int32)
    for sid, w in wnid_by_id.items():
        table[sid] = rank[w]
    return table


@lru_cache(maxsize=1)
def _ground_truth_ids() -> np.ndarray:
    return np.loadtxt(GROUND_TRUTH, dtype=np.int32)


def label(index: int) -> int:
    """1-based validation index → class 0..999 (WNID alphabetical order, no background)."""
    return int(_id_to_class()[_ground_truth_ids()[index - 1]])
