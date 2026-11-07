"""TLM (block dataflow, layouts, addressing) is bit-exact with the golden model on all layers."""
from __future__ import annotations

import numpy as np
import pytest

from compiler.analysis.tlm import Ddr, run_instr
from compiler.codegen.isa import Instr
from compiler.codegen.layout import c8, unpack_fmap
from compiler.codegen.program import build_program
from compiler.golden.run import run_model
from compiler.model.ir import load_model


@pytest.mark.parametrize("name", ["efficientnet-lite0", "mobilenet_v2", "resnet50"])
def test_tlm_matches_golden(name):
    model = load_model(name)
    x = np.random.default_rng(1).integers(-128, 128, model.input_shape).astype(np.int8)
    prog = build_program(model, x)
    gold = run_model(model, x)
    ddr = Ddr(prog.regions)
    for lay, ins in zip(model.layers, prog.instrs):
        assert Instr.decode(ins.encode()) == ins
        run_instr(ddr, Instr.decode(ins.encode()))
        nbytes = lay.h_out * lay.w_out * c8(lay.c_out)
        got = unpack_fmap(ddr.read(ins.out_base, -(-nbytes // 8) * 8), lay.h_out, lay.w_out, lay.c_out)
        np.testing.assert_array_equal(got, gold[lay.output], err_msg=f"layer {lay.index} {lay.kind}")
        # output padding channels are zp_out
        raw = np.frombuffer(ddr.read(ins.out_base, -(-nbytes // 8) * 8), np.int8)[:nbytes]
        pad = raw.reshape(lay.h_out * lay.w_out, c8(lay.c_out))[:, lay.c_out:]
        assert (pad == lay.zp_out).all() or lay.kind == "AVGPOOL" or pad.size == 0, lay.index
