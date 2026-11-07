"""Model → instruction stream + DDR image (memory map).

    prog = build_program(load_model("resnet50"), x_int8)
    prog.ddr  : dict[base] -> bytes region, prog.instrs: list[Instr], prog.tensor_addr: tensor id -> addr
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from compiler.codegen.isa import KINDS, Instr
from compiler.codegen.layout import fmap_bytes, pack_coef, pack_conv_weights, pack_dw_weights, pack_fmap
from compiler.model.ir import Layer, Model

DDR_BASE = 0x1000_0000
ALIGN = 64


def _align(v: int) -> int:
    return -(-v // ALIGN) * ALIGN


@dataclass
class Program:
    model: Model
    instrs: list[Instr]
    instr_addr: int
    regions: dict[int, bytes]               # initial DDR contents (address → bytes)
    tensor_addr: dict[int, int] = field(default_factory=dict)
    end_addr: int = 0

    def instr_blob(self) -> bytes:
        return b"".join(i.encode() for i in self.instrs)


def layer_instr(lay: Layer) -> Instr:
    return Instr(
        layer_type=KINDS[lay.kind], kh=lay.kh, kw=lay.kw, stride=lay.stride,
        pad_t=lay.pad_t, pad_l=lay.pad_l, pad_b=lay.pad_b, pad_r=lay.pad_r, in_dense=int(lay.in_dense),
        h_in=lay.h_in, w_in=lay.w_in, h_out=lay.h_out, w_out=lay.w_out, c_in=lay.c_in, c_out=lay.c_out,
        zp_in=lay.zp_in, zp_in2=lay.zp_in2, zp_out=lay.zp_out, act_min=lay.act_min, act_max=lay.act_max,
        pad_value=lay.pad_value, scalar0=lay.scalar0, scalar1=lay.scalar1 & 0xFFFFFFFF,
    )


def build_program(model: Model, x: np.ndarray | None = None, base: int = DDR_BASE) -> Program:
    """x: first-layer input int8 [H,W,C] (None reserves the input region only)."""
    cur = base
    regions: dict[int, bytes] = {}

    def alloc(nbytes: int) -> int:
        nonlocal cur
        addr = cur
        cur = _align(cur + nbytes)
        return addr

    instr_addr = alloc(64 * len(model.layers))
    tensor_addr: dict[int, int] = {}
    h, w, c = model.input_shape
    first_dense = model.layers[0].in_dense
    tensor_addr[model.input_id] = alloc(fmap_bytes(h, w, c, first_dense))
    if x is not None:
        regions[tensor_addr[model.input_id]] = pack_fmap(x, model.input_zp, first_dense)

    instrs: list[Instr] = []
    for lay in model.layers:
        ins = layer_instr(lay)
        ins.in_base = tensor_addr[lay.inputs[0]]
        if lay.kind == "ADD":
            ins.in2_base = tensor_addr[lay.inputs[1]]
        elif lay.kind in ("CONV", "DWCONV"):
            wbytes = pack_conv_weights(lay) if lay.kind == "CONV" else pack_dw_weights(lay)
            ins.in2_base = alloc(len(wbytes))
            regions[ins.in2_base] = wbytes
            coef = pack_coef(lay)
            ins.coef_base = alloc(len(coef))
            regions[ins.coef_base] = coef
        tensor_addr[lay.output] = alloc(fmap_bytes(lay.h_out, lay.w_out, lay.c_out))
        ins.out_base = tensor_addr[lay.output]
        instrs.append(ins)
    regions[instr_addr] = b"".join(i.encode() for i in instrs)
    return Program(model, instrs, instr_addr, regions, tensor_addr, cur)
