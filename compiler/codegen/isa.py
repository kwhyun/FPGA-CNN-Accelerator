"""512-bit layer instruction encode/decode (docs/ISA.md)."""
from __future__ import annotations

from dataclasses import dataclass, fields

KINDS = {"CONV": 0, "DWCONV": 1, "ADD": 2, "MAXPOOL": 3, "AVGPOOL": 4}
KIND_NAMES = {v: k for k, v in KINDS.items()}

# (field, word, lsb, width, signed)
FIELDS = [
    ("layer_type", 0, 60, 4, False), ("kh", 0, 56, 4, False), ("kw", 0, 52, 4, False),
    ("stride", 0, 48, 4, False), ("pad_t", 0, 44, 4, False), ("pad_l", 0, 40, 4, False),
    ("pad_b", 0, 36, 4, False), ("pad_r", 0, 32, 4, False), ("in_dense", 0, 31, 1, False),
    ("h_in", 1, 52, 12, False), ("w_in", 1, 40, 12, False), ("h_out", 1, 28, 12, False),
    ("w_out", 1, 16, 12, False),
    ("c_in", 2, 52, 12, False), ("c_out", 2, 40, 12, False), ("zp_in", 2, 32, 8, True),
    ("zp_in2", 2, 24, 8, True), ("zp_out", 2, 16, 8, True), ("act_min", 2, 8, 8, True),
    ("act_max", 2, 0, 8, True),
    ("pad_value", 3, 56, 8, True),
    ("scalar0", 4, 32, 32, False), ("scalar1", 4, 0, 32, False),
    ("in_base", 5, 32, 32, False), ("in2_base", 5, 0, 32, False),
    ("coef_base", 6, 32, 32, False), ("out_base", 6, 0, 32, False),
]


@dataclass
class Instr:
    layer_type: int = 0
    kh: int = 1
    kw: int = 1
    stride: int = 1
    pad_t: int = 0
    pad_l: int = 0
    pad_b: int = 0
    pad_r: int = 0
    in_dense: int = 0
    h_in: int = 1
    w_in: int = 1
    h_out: int = 1
    w_out: int = 1
    c_in: int = 1
    c_out: int = 1
    zp_in: int = 0
    zp_in2: int = 0
    zp_out: int = 0
    act_min: int = -128
    act_max: int = 127
    pad_value: int = 0
    scalar0: int = 0
    scalar1: int = 0
    in_base: int = 0
    in2_base: int = 0
    coef_base: int = 0
    out_base: int = 0

    def encode(self) -> bytes:
        words = [0] * 8
        for name, word, lsb, width, signed in FIELDS:
            v = int(getattr(self, name))
            lo, hi = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if signed else (0, (1 << width) - 1)
            if not lo <= v <= hi:
                raise ValueError(f"{name}={v} out of range")
            words[word] |= (v & ((1 << width) - 1)) << lsb
        return b"".join(w.to_bytes(8, "little") for w in words)

    @classmethod
    def decode(cls, blob: bytes) -> "Instr":
        words = [int.from_bytes(blob[8 * i: 8 * i + 8], "little") for i in range(8)]
        kw = {}
        for name, word, lsb, width, signed in FIELDS:
            v = (words[word] >> lsb) & ((1 << width) - 1)
            if signed and v >> (width - 1):
                v -= 1 << width
            kw[name] = v
        return cls(**kw)


def scalar1_as_int(v: int) -> int:
    return v - (1 << 32) if v >> 31 else v
