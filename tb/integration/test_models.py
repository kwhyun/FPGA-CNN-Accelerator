"""Full model instruction streams; check every tensor, including residuals."""
from __future__ import annotations

import os
from pathlib import Path

import cocotb
import numpy as np
from cocotb.clock import Clock

from tb.common.sim import run_cocotb
from tb.integration.test_layers import Bench, SOURCES
from compiler.codegen.layout import pack_fmap
from compiler.codegen.program import build_program
from compiler.golden.run import run_model
from compiler.model.ir import load_model


async def check_model(dut, name, seed):
    bench = Bench(dut)
    await bench.reset()
    model = load_model(name)
    rng = np.random.default_rng(seed)
    x = rng.integers(-128, 128, model.input_shape, dtype=np.int8)
    expected = run_model(model, x)
    prog = build_program(model, x)
    cycles = await bench.execute(prog, timeout_cycles=150_000_000)
    assert bench.load_compute_overlap, "loaders never overlapped compute"
    assert bench.compute_drain_overlap, "drain never overlapped compute"
    for layer, ins in zip(model.layers, prog.instrs):
        assert bench.output(ins, layer) == pack_fmap(expected[layer.output], layer.zp_out), (
            name, layer.index, layer.kind)
    print(f"MODEL {name}: cycles={cycles}, layers={len(model.layers)}", flush=True)


@cocotb.test()
async def efficientnet(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns", impl="gpi").start())
    await check_model(dut, "efficientnet-lite0", 7020)


@cocotb.test(skip=os.environ.get("RUN_SLOW_MODELS") != "1")
async def mobilenet(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns", impl="gpi").start())
    await check_model(dut, "mobilenet_v2", 7021)


@cocotb.test(skip=os.environ.get("RUN_SLOW_MODELS") != "1")
async def resnet(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns", impl="gpi").start())
    await check_model(dut, "resnet50", 7022)


def test_models(monkeypatch):
    from tb.common import sim

    # Optimize the large cycle-accurate model without changing the shared runner.
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.setattr(sim, "VERILATOR_ARGS", [*sim.VERILATOR_ARGS, "-CFLAGS", "-O3"])
    run_cocotb("accel_top", SOURCES, "tb.integration.test_models", build_name=Path(__file__).stem)
