"""Whole-model integer golden run."""
from __future__ import annotations

import numpy as np

from compiler.golden.layers import run_layer
from compiler.model.ir import Model


def _as_hwc(t: np.ndarray, h: int, w: int, c: int) -> np.ndarray:
    return t.reshape(h, w, c)


def run_model(model: Model, x: np.ndarray) -> dict[int, np.ndarray]:
    """x: first-layer input int8 [H, W, C]. Returns tensor id → int8 [H, W, C]."""
    tensors: dict[int, np.ndarray] = {model.input_id: np.asarray(x, dtype=np.int8)}
    for layer in model.layers:
        ops = [_as_hwc(tensors[i], layer.h_in, layer.w_in, layer.c_in) for i in layer.inputs]
        tensors[layer.output] = run_layer(layer, *ops)
    return tensors


def logits(model: Model, x: np.ndarray) -> np.ndarray:
    return run_model(model, x)[model.output_id].reshape(-1)


def topk(scores: np.ndarray, k: int = 5) -> np.ndarray:
    """Descending; ties go to the lower index."""
    order = np.lexsort((np.arange(scores.size), -scores.astype(np.int64)))
    return order[:k]
