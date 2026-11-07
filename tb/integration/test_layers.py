"""Real model layers through CSR and three AXI ports sharing one DDR image."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer
from cocotbext.axi import AxiLiteBus, AxiLiteMaster, AxiReadBus, AxiWriteBus, AxiRamRead, AxiRamWrite
from cocotbext.axi.memory import SparseMemory

from tb.common.sim import ROOT, run_cocotb
from compiler.analysis.tiling import HwConfig, plan_layer
from compiler.codegen.layout import c8, pack_fmap
from compiler.codegen.program import build_program
from compiler.golden.layers import run_layer
from compiler.model.ir import load_model

SOURCES = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "rtl").glob("*.sv"))
CASES = [
    ("efficientnet-lite0", 0),   # dense 3x3 s2, n_outer
    ("resnet50", 0),            # dense 7x7 s2
    ("resnet50", 4),            # 3x3 s1, multiple K banks
    ("efficientnet-lite0", 2),   # 1x1 n_outer
    ("efficientnet-lite0", 12),  # q=3 and an odd output tile
    ("efficientnet-lite0", 22),  # 1x1 m_outer, k_blks > 1
    ("resnet50", 33),           # subsampled 1x1 s2, k_blks > 1
    ("efficientnet-lite0", 59),  # FC
    ("efficientnet-lite0", 1),   # DW 3x3 s1
    ("efficientnet-lite0", 4),   # DW 3x3 s2
    ("efficientnet-lite0", 11),  # DW 5x5 s2
    ("efficientnet-lite0", 9),   # ADD, multiple chunks
    ("resnet50", 1),            # MAXPOOL
    ("efficientnet-lite0", 58),  # AVGPOOL
]


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.load_compute_overlap = False
        self.compute_drain_overlap = False
        self.mem = SparseMemory(1 << 32)
        self.csr = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_ni,
                                 reset_active_level=False)
        self.readers = [AxiRamRead(AxiReadBus.from_prefix(dut, prefix), dut.clk, dut.rst_ni,
                                  reset_active_level=False, mem=self.mem)
                        for prefix in ("m_axi_in", "m_axi_w")]
        self.writer = AxiRamWrite(AxiWriteBus.from_prefix(dut, "m_axi_out"), dut.clk, dut.rst_ni,
                                 reset_active_level=False, mem=self.mem)
        for bus in [*self.readers, self.writer]:
            bus.log.setLevel(logging.WARNING)
        self.csr.write_if.log.setLevel(logging.WARNING)
        self.csr.read_if.log.setLevel(logging.WARNING)

    async def reset(self):
        self.dut.rst_ni.value = 0
        await ClockCycles(self.dut.clk, 5)
        self.dut.rst_ni.value = 1
        await ClockCycles(self.dut.clk, 5)

    async def execute(self, prog, instructions=None, timeout_cycles=30_000_000):
        for address, data in prog.regions.items():
            self.mem[address:address + len(data)] = data
        instructions = prog.instrs if instructions is None else instructions
        blob = b"".join(ins.encode() for ins in instructions)
        self.mem[prog.instr_addr:prog.instr_addr + len(blob)] = blob
        await self.csr.write_dword(0x10, prog.instr_addr)
        await self.csr.write_dword(0x14, len(instructions))
        await self.csr.write_dword(0x08, 3)
        await self.csr.write_dword(0x0C, 3)
        await self.csr.write_dword(0x00, 1)
        for poll in range((timeout_cycles + 4095) // 4096):
            await Timer(40960, unit="ns")
            if not int(self.dut.compute_idle.value):
                self.load_compute_overlap |= bool(int(self.dut.input_pending_q.value) or
                                                  int(self.dut.weight_pending_q.value))
                self.compute_drain_overlap |= not int(self.dut.drain_idle.value)
            status = await self.csr.read_dword(0x04)
            if poll and poll % 512 == 0:
                print(f"PROGRESS layer={await self.csr.read_dword(0x18)} cycles~{4096 * poll}", flush=True)
            if status & 6:
                assert status == 2, (status, await self.csr.read_dword(0x1C),
                                     await self.csr.read_dword(0x18))
                assert int(self.dut.irq_o.value) == 1
                await self.csr.write_dword(0x20, 0)
                cycles = await self.csr.read_dword(0x24)
                await self.csr.write_dword(0x0C, 1)
                assert await self.csr.read_dword(0x0C) == 0
                return cycles
        for module in ("u_iterator0", "u_iterator1", "u_iterator2", "u_iterator3",
                       "u_input_pingpong", "u_weight_pingpong", "u_coefficient_pingpong", "u_accumulator_pingpong",
                       "u_compute", "u_drain", "u_matrix_agu", "u_weight_loader"):
            obj = getattr(self.dut, module)
            values = {}
            for sig in obj:
                if sig._name.endswith("_q"):
                    try:
                        values[sig._name] = int(sig.value)
                    except (AttributeError, TypeError, ValueError):
                        pass
            print("TIMEOUT", module, values, flush=True)
        raise AssertionError(f"accelerator timed out at layer {await self.csr.read_dword(0x18)}")

    def output(self, ins, layer):
        n = layer.h_out * layer.w_out * c8(layer.c_out)
        return bytes(self.mem[ins.out_base:ins.out_base + n])


@cocotb.test()
async def real_layers(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns", impl="gpi").start())
    bench = Bench(dut)
    await bench.reset()
    assert await bench.csr.read_dword(0x28) == 0x00010000
    rng = np.random.default_rng(0x7020)
    selected = os.environ.get("LAYER_CASES")
    cases = CASES if selected is None else [CASES[int(i)] for i in selected.split(",")]
    for name, index in cases:
        model = load_model(name)
        layer = model.layers[index]
        shape = (layer.h_in, layer.w_in, layer.c_in)
        x = rng.integers(-128, 128, shape, dtype=np.int8)
        prog = build_program(model)
        ins = prog.instrs[index]
        # A non-first instruction reads the original tensor address.
        prog.regions[ins.in_base] = pack_fmap(x, layer.zp_in, layer.in_dense)
        if layer.kind == "ADD":
            b = rng.integers(-128, 128, shape, dtype=np.int8)
            prog.regions[ins.in2_base] = pack_fmap(b, layer.zp_in2)
            expected = run_layer(layer, x, b)
        else:
            expected = run_layer(layer, x)
        cycles = await bench.execute(prog, [ins], timeout_cycles=max(500_000, plan_layer(layer, HwConfig()).cycles * 4))
        got = bench.output(ins, layer)
        reference = pack_fmap(expected, layer.zp_out)
        if got != reference:
            aa = np.frombuffer(got, np.int8)
            bb = np.frombuffer(reference, np.int8)
            bad = np.flatnonzero(aa != bb)
            raise AssertionError(f"{name} L{index} {layer.kind}: {len(bad)} mismatches; "
                                 f"first {bad[:12]} got {aa[bad[:12]]} expected {bb[bad[:12]]}")
        estimate = plan_layer(layer, HwConfig()).cycles
        print(f"LAYER {name} L{index} {layer.kind}: cycles={cycles} estimate={estimate}", flush=True)


def test_layers(monkeypatch):
    from tb.common import sim

    # Optimize the large cycle-accurate model without changing the shared runner.
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.setattr(sim, "VERILATOR_ARGS", [*sim.VERILATOR_ARGS, "-CFLAGS", "-O3"])
    run_cocotb("accel_top", SOURCES, "tb.integration.test_layers", build_name=Path(__file__).stem)


@cocotb.test()
async def directed_and_stalled(dut):
    """Odd tiles, channel padding, AVG chunks, and randomized AXI backpressure."""
    from dataclasses import replace
    from itertools import cycle
    from compiler.model.ir import Layer, Model, mant_shift, mult_word

    Clock(dut.clk, 10, unit="ns", impl="gpi").start()
    bench = Bench(dut)
    await bench.reset()
    bench.csr.write_if.aw_channel.set_pause_generator(cycle([0, 0, 1, 1, 1]))
    bench.csr.write_if.w_channel.set_pause_generator(cycle([1, 0, 0, 1, 0]))
    bench.csr.write_if.b_channel.set_pause_generator(cycle([0, 1, 0, 1, 0]))
    bench.csr.read_if.r_channel.set_pause_generator(cycle([0, 1, 0, 0, 1]))
    for reader in bench.readers:
        reader.ar_channel.set_pause_generator(cycle([0, 1, 0, 0, 1, 0]))
        reader.r_channel.set_pause_generator(cycle([0, 0, 1, 0, 1, 0, 0]))
    bench.writer.aw_channel.set_pause_generator(cycle([0, 1, 1, 0, 0]))
    bench.writer.w_channel.set_pause_generator(cycle([0, 0, 1, 0, 1]))
    bench.writer.b_channel.set_pause_generator(cycle([1, 1, 0, 0, 0]))
    rng = np.random.default_rng(0xD1EC7)
    word = mult_word(*mant_shift(1 / 64))
    layers = [
        Layer(0, "dense_odd_q3", "CONV", [0], 1, 4, 8, 3, 4, 8, 40,
              kh=3, kw=3, pad_t=1, pad_l=1, pad_b=1, pad_r=1,
              in_dense=True, zp_in=-7, pad_value=-7, zp_out=-3),
        Layer(0, "dw_odd_group", "DWCONV", [0], 1, 5, 7, 17, 3, 4, 17,
              kh=5, kw=5, stride=2, pad_t=2, pad_l=2, pad_b=2, pad_r=2,
              zp_in=11, pad_value=11, zp_out=-2),
        Layer(0, "avg_group_chunks", "AVGPOOL", [0], 1, 64, 64, 24, 1, 1, 24,
              zp_in=-9, zp_out=3, scalar0=mult_word(*mant_shift(1 / 4096)),
              scalar1=4096 * 9),
        Layer(0, "add_extremes", "ADD", [0, 2], 1, 4, 9, 17, 4, 9, 17,
              zp_in=-3, zp_in2=6, zp_out=4, scalar0=mult_word(*mant_shift(.5)),
              scalar1=32768),
        Layer(0, "max_padding", "MAXPOOL", [0], 1, 3, 4, 17, 3, 4, 17,
              kh=3, kw=3, pad_t=1, pad_l=1, pad_b=1, pad_r=1,
              zp_in=-13, zp_out=-13, pad_value=-128),
    ]
    for layer in layers:
        if layer.kind in ("CONV", "DWCONV"):
            shape = ((layer.c_out, layer.kh, layer.kw, layer.c_in) if layer.kind == "CONV"
                     else (layer.kh, layer.kw, layer.c_in))
            layer = replace(layer, weights=rng.integers(-12, 13, shape, dtype=np.int8),
                            eff_bias=rng.integers(-1000, 1001, layer.c_out, dtype=np.int32),
                            mult=np.full(layer.c_out, word, dtype=np.int32))
        model = Model(layer.name, [layer], 0, (layer.h_in, layer.w_in, layer.c_in),
                      1., layer.zp_in, 1, layer.c_out, 0)
        x = rng.choice(np.array([-128, -127, -1, 0, 1, 126, 127], np.int8), model.input_shape)
        # build_program allocates ADD's second input from the tensor table; a
        # preceding identity tensor alias supplies that address for this fixture.
        if layer.kind == "ADD":
            original = load_model("efficientnet-lite0")
            prog = build_program(original)
            ins = prog.instrs[9]
            from compiler.codegen.program import layer_instr
            small = layer_instr(layer)
            small.in_base, small.in2_base, small.out_base = ins.in_base, ins.in2_base, ins.out_base
            prog.instrs = [small]
            prog.regions[small.in_base] = pack_fmap(x, layer.zp_in)
            b = np.flip(x, axis=1).copy()
            prog.regions[small.in2_base] = pack_fmap(b, layer.zp_in2)
            expected = run_layer(layer, x, b)
        else:
            prog = build_program(model, x)
            expected = run_layer(layer, x)
        await bench.execute(prog, timeout_cycles=500_000)
        assert bench.output(prog.instrs[0], layer) == pack_fmap(expected, layer.zp_out), layer.name
    # CSR byte strobes and zero-length/invalid programs exercise control paths.
    await bench.csr.write(0x10, b"\x11\x22\x33\x44")
    await bench.csr.write(0x11, b"\xaa")
    assert await bench.csr.read_dword(0x10) == 0x4433AA11
    await bench.csr.write_dword(0x14, 0)
    await bench.csr.write_dword(0, 1)
    await ClockCycles(dut.clk, 10)
    assert await bench.csr.read_dword(4) == 2
    from compiler.codegen.isa import Instr
    bad = Instr(layer_type=15)
    bench.mem[0x10000000:0x10000040] = bad.encode()
    await bench.csr.write_dword(0x10, 0x10000000)
    await bench.csr.write_dword(0x14, 1)
    await bench.csr.write_dword(0, 1)
    await ClockCycles(dut.clk, 200)
    assert await bench.csr.read_dword(4) == 4
    assert await bench.csr.read_dword(0x1C) == 2
    impossible = Instr(h_in=1, w_in=1, h_out=1, w_out=1024, c_in=8, c_out=64)
    bench.mem[0x10000000:0x10000040] = impossible.encode()
    await bench.csr.write_dword(0, 1)
    for _ in range(32):
        await ClockCycles(dut.clk, 256)
        if await bench.csr.read_dword(4) == 4:
            break
    else:
        raise AssertionError("planner error was not reported")
    assert await bench.csr.read_dword(0x1C) == 1
    original_read = bench.readers[0]._read

    async def faulty_read(address, length):
        if address == 0x10000000:
            raise IOError("directed instruction-fetch SLVERR")
        return await original_read(address, length)

    bench.readers[0]._read = faulty_read
    await bench.csr.write_dword(0, 1)
    await ClockCycles(dut.clk, 300)
    bench.readers[0]._read = original_read
    assert await bench.csr.read_dword(4) == 4
    assert await bench.csr.read_dword(0x1C) == 3
    await bench.csr.write_dword(0, 2)
    await ClockCycles(dut.clk, 20)
    assert await bench.csr.read_dword(0x1C) == 0
    # Abort while the first layer is running. Accepted reads/writes must drain;
    # the following instruction must never write its sentinel output region.
    from dataclasses import replace
    model = load_model("efficientnet-lite0")
    x = np.full(model.input_shape, model.input_zp, dtype=np.int8)
    prog = build_program(model, x)
    first = prog.instrs[0]
    sentinel = 0x1F000000
    second = replace(first, out_base=sentinel)
    for address, data in prog.regions.items():
        bench.mem[address:address + len(data)] = data
    bench.mem[prog.instr_addr:prog.instr_addr + 128] = first.encode() + second.encode()
    bench.mem[sentinel:sentinel + 64] = bytes([0xA5] * 64)
    await bench.csr.write_dword(0x10, prog.instr_addr)
    await bench.csr.write_dword(0x14, 2)
    await bench.csr.write_dword(0, 1)
    await Timer(50000, unit="ns")
    assert await bench.csr.read_dword(4) == 1
    await bench.csr.write_dword(0, 2)
    for _ in range(256):
        await Timer(40960, unit="ns")
        if await bench.csr.read_dword(4) == 0:
            break
    else:
        raise AssertionError("soft reset did not fence the active layer")
    assert bytes(bench.mem[sentinel:sentinel + 64]) == bytes([0xA5] * 64)
    assert int(dut.input_pending_q.value) == 0
    assert int(dut.weight_pending_q.value) == 0
    assert int(dut.output_dma_idle.value) == 1
