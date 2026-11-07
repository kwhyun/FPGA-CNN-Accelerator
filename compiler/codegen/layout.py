"""DDR byte layouts (docs/ISA.md "DDR layout")."""
from __future__ import annotations

import numpy as np

from compiler.model.ir import Layer


def c8(c: int) -> int:
    return -(-c // 8) * 8


def pixel_stride(c: int, dense: bool) -> int:
    return c if dense else c8(c)


def pack_fmap(x: np.ndarray, zp: int, dense: bool = False) -> bytes:
    """[H, W, C] int8 → NHWC, pixel stride C8(C) (C if dense), padding channels = zp."""
    h, w, c = x.shape
    cs = pixel_stride(c, dense)
    buf = np.full((h, w, cs), zp, dtype=np.int8)
    buf[:, :, :c] = x
    return buf.tobytes()


def unpack_fmap(data: bytes, h: int, w: int, c: int, dense: bool = False) -> np.ndarray:
    cs = pixel_stride(c, dense)
    return np.frombuffer(data, dtype=np.int8, count=h * w * cs).reshape(h, w, cs)[:, :, :c].copy()


def fmap_bytes(h: int, w: int, c: int, dense: bool = False) -> int:
    return h * w * pixel_stride(c, dense)


def conv_kchunks(lay: Layer) -> int:
    """Number of K slices (8 B) in one kernel row = ceil(kw·Cs/8)."""
    return -(-lay.kw * pixel_stride(lay.c_in, lay.in_dense) // 8)


def pack_conv_weights(lay: Layer) -> bytes:
    """[n_tile][kr][kk] 8 B rows (8 output channels). kk → (kc, c) = divmod(kk, Cs)."""
    cs = pixel_stride(lay.c_in, lay.in_dense)
    kc_n = conv_kchunks(lay)
    n_tiles = c8(lay.c_out) // 8
    w = lay.weights.astype(np.int8)  # [Cout, kh, kw, Cin]
    out = np.zeros((n_tiles, lay.kh, kc_n * 8, 8), dtype=np.int8)
    span = np.zeros((c8(lay.c_out), lay.kh, kc_n * 8), dtype=np.int8)
    for kc in range(lay.kw):
        span[: lay.c_out, :, kc * cs: kc * cs + lay.c_in] = w[:, :, kc, :]
    out[:] = span.reshape(n_tiles, 8, lay.kh, kc_n * 8).transpose(0, 2, 3, 1)
    return out.tobytes()


def pack_dw_weights(lay: Layer) -> bytes:
    """[g][kr][kc] 8 B (channels 8g..8g+7)."""
    groups = c8(lay.c_in) // 8
    w = np.zeros((lay.kh, lay.kw, groups * 8), dtype=np.int8)
    w[:, :, : lay.c_in] = lay.weights
    return w.reshape(lay.kh, lay.kw, groups, 8).transpose(2, 0, 1, 3).tobytes()


def pack_coef(lay: Layer) -> bytes:
    """Per channel {eff_bias:int32, mult word:int32} little-endian, zero-padded to C8(c_out)."""
    n = c8(lay.c_out)
    arr = np.zeros((n, 2), dtype="<i4")
    arr[: lay.c_out, 0] = lay.eff_bias
    arr[: lay.c_out, 1] = lay.mult
    return arr.tobytes()
