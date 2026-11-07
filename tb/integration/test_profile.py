"""Per-layer RTL profile: perf counters snapshotted at every layer boundary.

    PROFILE_MODEL=resnet50 pytest -q -s tb/integration/test_profile.py
Writes build/<model>/profile.json. The input is the board's last image (ImageNet val #1000).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import Edge, RisingEdge

from tb.common.sim import run_cocotb
from tb.integration.test_layers import SOURCES, Bench
from compiler.codegen.layout import pack_fmap
from compiler.codegen.program import build_program
from compiler.golden.run import run_model
from compiler.model.config import model_build_dir, model_config
from compiler.model.inputs import first_layer_input, load_image
from compiler.model.ir import load_model

NAMES = ["total", "rows", "hp_in", "hp_w", "hp_out", "agu_wait", "drain_wait"]


def _perf(dut) -> list[int]:
    return [int(dut.u_control.perf_q[i].value) for i in range(len(NAMES))]


@cocotb.test()
async def profile(dut):
    name = os.environ["PROFILE_MODEL"]
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns", impl="gpi").start())
    bench = Bench(dut)
    await bench.reset()
    model = load_model(name)
    x = first_layer_input(model, load_image(model_config(name), 1000))
    prog = build_program(model, x)
    snaps: list[list[int]] = []

    async def monitor():
        last = -1
        while True:
            await Edge(dut.u_layer.layer_index_o)
            await RisingEdge(dut.clk)
            idx = int(dut.u_layer.layer_index_o.value)
            if idx != last:
                snaps.append(_perf(dut))
                last = idx

    mon = cocotb.start_soon(monitor())
    cycles = await bench.execute(prog, timeout_cycles=150_000_000)
    mon.cancel()
    final = _perf(dut)
    expected = run_model(model, x)
    for layer, ins in zip(model.layers, prog.instrs):
        assert bench.output(ins, layer) == pack_fmap(expected[layer.output], layer.zp_out), layer.index
    # snaps[k] = counters at the start of layer k (first increment = layer 1 start). Layer 0 starts at 0.
    bounds = [[0] * len(NAMES)] + snaps[: len(model.layers) - 1] + [final]
    layers = []
    for i, lay in enumerate(model.layers):
        d = {n: bounds[i + 1][k] - bounds[i][k] for k, n in enumerate(NAMES)}
        macs = lay.h_out * lay.w_out * lay.c_out * lay.kh * lay.kw * (lay.c_in if lay.kind == "CONV" else 1)
        if lay.kind in ("ADD", "MAXPOOL", "AVGPOOL"):
            macs = 0
        layers.append({"layer": i, "kind": lay.kind, "k": lay.kh, "stride": lay.stride,
                       "shape": [lay.h_in, lay.w_in, lay.c_in, lay.h_out, lay.w_out, lay.c_out], "macs": macs, **d})
    out = model_build_dir(name) / "profile.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": name, "image": 1000, "cycles": cycles,
                               "totals": dict(zip(NAMES, final)), "layers": layers}, indent=1))
    print(f"PROFILE {name}: cycles={cycles} totals={dict(zip(NAMES, final))}", flush=True)


def test_profile(monkeypatch):
    from tb.common import sim

    name = os.environ.get("PROFILE_MODEL", "efficientnet-lite0")
    monkeypatch.setenv("PROFILE_MODEL", name)
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.setattr(sim, "VERILATOR_ARGS", [*sim.VERILATOR_ARGS, "-CFLAGS", "-O3", "--public-flat-rw"])
    run_cocotb("accel_top", SOURCES, "tb.integration.test_profile", build_name=f"test_profile_{name}")
