"""Integer golden kernels — exactly as defined in docs/arithmetic.md.

Inputs/outputs are logical NHWC int8 arrays ``[H, W, C]`` without channel padding.
Accumulation uses float64 matmul, checked for exactness (|partial sum| < 2^52), then converted to int64.
"""
from __future__ import annotations

import numpy as np

from compiler.model.ir import Layer, word_ms

_EXACT = float(1 << 52)


def round_shift(x: np.ndarray, s: np.ndarray | int) -> np.ndarray:
    """sign(x)·floor((|x| + 2^(S−1)) / 2^S), int64."""
    x = np.asarray(x, dtype=np.int64)
    s = np.asarray(s, dtype=np.int64)
    half = np.left_shift(np.int64(1), s - 1)
    mag = np.right_shift(np.abs(x) + half, s)
    return np.where(x < 0, -mag, mag)


def clamp8(v: np.ndarray, layer: Layer) -> np.ndarray:
    return np.clip(v, layer.act_min, layer.act_max).astype(np.int8)


def pad_input(x: np.ndarray, layer: Layer) -> np.ndarray:
    return np.pad(
        x.astype(np.int16),
        ((layer.pad_t, layer.pad_b), (layer.pad_l, layer.pad_r), (0, 0)),
        constant_values=layer.pad_value,
    )


def _windows(xp: np.ndarray, layer: Layer) -> np.ndarray:
    """[H_out, W_out, kh, kw, C] windows (view)."""
    s = layer.stride
    v = np.lib.stride_tricks.sliding_window_view(xp, (layer.kh, layer.kw), axis=(0, 1))
    v = v[:: s, :: s][: layer.h_out, : layer.w_out]  # [Ho, Wo, C, kh, kw]
    return np.moveaxis(v, 2, 4)  # [Ho, Wo, kh, kw, C]


def _check_shape(x: np.ndarray, layer: Layer) -> None:
    if x.shape != (layer.h_in, layer.w_in, layer.c_in):
        raise ValueError(f"layer {layer.index}: input {x.shape} != {(layer.h_in, layer.w_in, layer.c_in)}")


def _exact_matmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    bound = float(np.abs(a).max(initial=0)) * float(np.abs(b).sum(axis=0).max(initial=0))
    if bound >= _EXACT:
        raise ValueError("float64 accumulation would not be exact")
    return np.rint(a.astype(np.float64) @ b.astype(np.float64)).astype(np.int64)


def requant(acc: np.ndarray, layer: Layer) -> np.ndarray:
    """acc: int64 [..., Cout] → int8 with per-channel {eff_bias, M, S}."""
    words = layer.mult.astype(np.int64)
    m = words & 0x1FFFF
    s = (words >> 24) & 0x3F
    y = round_shift((acc + layer.eff_bias.astype(np.int64)) * m, s) + layer.zp_out
    return clamp8(y, layer)


def conv(x: np.ndarray, layer: Layer) -> np.ndarray:
    _check_shape(x, layer)
    win = _windows(pad_input(x, layer), layer)  # [Ho, Wo, kh, kw, Cin]
    a = win.reshape(layer.h_out * layer.w_out, -1)
    w = layer.weights.reshape(layer.c_out, -1).T  # [kh*kw*Cin, Cout]
    acc = _exact_matmul(a, w).reshape(layer.h_out, layer.w_out, layer.c_out)
    return requant(acc, layer)


def dwconv(x: np.ndarray, layer: Layer) -> np.ndarray:
    _check_shape(x, layer)
    win = _windows(pad_input(x, layer), layer).astype(np.int64)  # [Ho, Wo, kh, kw, C]
    acc = np.einsum("hwijc,ijc->hwc", win, layer.weights.astype(np.int64))
    return requant(acc, layer)


def add(a: np.ndarray, b: np.ndarray, layer: Layer) -> np.ndarray:
    _check_shape(a, layer)
    if b.shape != a.shape:
        raise ValueError("ADD operand shape mismatch")
    ma, s = word_ms(layer.scalar0)
    mb = layer.scalar1
    v = (a.astype(np.int64) - layer.zp_in) * ma + (b.astype(np.int64) - layer.zp_in2) * mb
    return clamp8(round_shift(v, s) + layer.zp_out, layer)


def maxpool(x: np.ndarray, layer: Layer) -> np.ndarray:
    _check_shape(x, layer)
    win = _windows(pad_input(x, layer), layer)
    return clamp8(win.max(axis=(2, 3)), layer)


def avgpool(x: np.ndarray, layer: Layer) -> np.ndarray:
    _check_shape(x, layer)
    m, s = word_ms(layer.scalar0)
    total = x.astype(np.int64).sum(axis=(0, 1)) + layer.scalar1
    y = round_shift(total * m, s) + layer.zp_out
    return clamp8(y, layer).reshape(1, 1, layer.c_out)


def run_layer(layer: Layer, *operands: np.ndarray) -> np.ndarray:
    if layer.kind == "CONV":
        return conv(operands[0], layer)
    if layer.kind == "DWCONV":
        return dwconv(operands[0], layer)
    if layer.kind == "ADD":
        return add(operands[0], operands[1], layer)
    if layer.kind == "MAXPOOL":
        return maxpool(operands[0], layer)
    if layer.kind == "AVGPOOL":
        return avgpool(operands[0], layer)
    raise ValueError(f"unknown layer kind {layer.kind}")
