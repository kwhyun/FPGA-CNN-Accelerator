"""BRAM-backed vector blocks: real model scales, TLM ordering, and stalls."""
from dataclasses import replace
from pathlib import Path
import random

import cocotb
import numpy as np
from cocotb.triggers import Timer

from tb.common.sim import run_cocotb
from compiler.golden.layers import add, avgpool, maxpool
from compiler.model.ir import load_model, mant_shift, mult_word


SIGNALS = ("done_o", "busy_o", "v_valid_o", "v_ready_o", "v_a_o", "v_b_o",
           "v_first_o", "v_last_o", "out_valid_o", "out_ready_i", "out_data_o",
           "in_rd_en_o", "in_rd_addr0_o", "in_rd_addr1_o", "w_rd_en_o", "w_rd_addr_o")


def pack(row):
    return int.from_bytes(np.asarray(row, dtype=np.int8).tobytes(), "little")


def packed_rows(tensor):
    return [pack(row) for row in tensor.reshape(-1, 8)]


async def tick(dut):
    dut.clk.value = 0
    await Timer(5, unit="ns")
    sample = {name: int(getattr(dut, name).value) for name in SIGNALS}
    dut.clk.value = 1
    await Timer(5, unit="ns")
    return sample


async def reset(dut):
    dut.rst_ni.value = 0
    dut.start_i.value = 0
    dut.in_wr_en_i.value = 0
    dut.w_wr_en_i.value = 0
    dut.out_ready_i.value = 0
    for _ in range(3):
        sample = await tick(dut)
        assert sample["v_valid_o"] == sample["out_valid_o"] == 0
    dut.rst_ni.value = 1
    await tick(dut)
    assert int(dut.busy_o.value) == 0


def config(layer, **updates):
    cfg = {name + "_i": getattr(layer, name) for name in
           ("stride", "kh", "kw", "pad_t", "pad_l", "h_in", "w_in", "w_out", "pad_value")}
    cfg.update(op_i={"ADD": 0, "MAXPOOL": 1, "AVGPOOL": 2}[layer.kind],
               zp_a_i=layer.zp_in, zp_b_i=layer.zp_in2, zp_out_i=layer.zp_out,
               act_min_i=layer.act_min, act_max_i=layer.act_max,
               scalar0_i=layer.scalar0, scalar1_i=layer.scalar1,
               lo_i=0, oh0_i=0, rows_i=1, groups_i=layer.c_in // 8,
               npix_i=layer.h_in * layer.w_in, nrows_i=0)
    cfg.update(updates)
    return cfg


async def load(dut, aa, bb=()):
    assert len(aa) <= 8192 and len(bb) <= 2048
    for index in range(max(len(aa), len(bb))):
        dut.in_wr_en_i.value = int(index < len(aa))
        dut.w_wr_en_i.value = int(index < len(bb))
        if index < len(aa):
            dut.in_wr_addr_i.value = index
            dut.in_wr_data_i.value = aa[index]
        if index < len(bb):
            dut.w_wr_addr_i.value = index
            dut.w_wr_data_i.value = bb[index]
        await tick(dut)
    dut.in_wr_en_i.value = 0
    dut.w_wr_en_i.value = 0


async def run_block(dut, name, cfg, aa, beats, expected, rng, bb=(), stressed=False):
    """Each beat contains (a, b, first, last, RAM address or None for pad)."""
    for signal, value in cfg.items():
        handle = getattr(dut, signal)
        handle.value = int(value) & ((1 << len(handle)) - 1)
    await load(dut, aa, bb)
    dut.out_ready_i.value = 1
    dut.start_i.value = 1
    await tick(dut)
    dut.start_i.value = 0
    sent = received = reads = dones = 0
    cycles = []
    stalled = output_stalled = None
    read_addresses = [beat[4] for beat in beats if beat[4] is not None]
    was_final = False
    first_valid = None
    for cycle in range(len(beats) * 6 + 500):
        # Long stalls exercise FIFO reservation, beyond merely isolated bubbles.
        ready = not stressed or (cycle % 173 >= 47 and rng.random() < 0.66)
        dut.out_ready_i.value = int(ready)
        sample = await tick(dut)
        assert sample["done_o"] == int(was_final), (name, cycle, "done timing")
        if sample["done_o"]:
            dones += 1
            assert not sample["busy_o"]
        elif dones == 0:
            assert sample["busy_o"]
        was_final = False
        assert sample["in_rd_en_o"] in (0, 1)
        assert sample["in_rd_addr1_o"] == 0
        if sample["in_rd_en_o"]:
            assert reads < len(read_addresses)
            assert sample["in_rd_addr0_o"] == read_addresses[reads], (name, reads)
            reads += 1
        if cfg["op_i"] == 0:
            assert sample["w_rd_en_o"] == sample["in_rd_en_o"]
            if sample["w_rd_en_o"]:
                assert sample["w_rd_addr_o"] == sample["in_rd_addr0_o"]
        else:
            assert not sample["w_rd_en_o"]
        payload = tuple(sample[key] for key in ("v_a_o", "v_b_o", "v_first_o", "v_last_o"))
        if stalled is not None:
            assert sample["v_valid_o"] and payload == stalled, (name, "input stability")
        stalled = payload if sample["v_valid_o"] and not sample["v_ready_o"] else None
        if sample["v_valid_o"]:
            if first_valid is None:
                first_valid = cycle
            if sample["v_ready_o"]:
                assert sent < len(beats)
                assert payload == beats[sent][:4], (name, sent, payload, beats[sent][:4])
                sent += 1
                cycles.append(cycle)
                was_final = sent == len(beats)
        if output_stalled is not None:
            assert sample["out_valid_o"] and sample["out_data_o"] == output_stalled
        output_stalled = (sample["out_data_o"] if sample["out_valid_o"] and not ready else None)
        if sample["out_valid_o"] and ready:
            assert received < len(expected), (name, "extra result")
            assert sample["out_data_o"] == expected[received], (name, received)
            received += 1
        if received == len(expected) and dones:
            break
    else:
        raise AssertionError((name, "timeout", sent, len(beats), received, len(expected)))
    assert sent == len(beats) and reads == len(read_addresses) and dones == 1
    if not stressed:
        assert cycles == list(range(cycles[0], cycles[0] + len(beats))), (name, "throughput")
    # Include FIFO drain in latency checks, and reject duplicate results/done.
    dut.out_ready_i.value = 1
    for _ in range(12):
        sample = await tick(dut)
        assert not sample["done_o"] and not sample["busy_o"]
        assert not sample["out_valid_o"] and not sample["v_valid_o"]
    rate = len(beats) / (cycles[-1] - cycles[0] + 1)
    dut._log.info("%s: %d input rows, first transfer start+%d cycles, %.6f rows/cycle%s",
                  name, len(beats), first_valid + 1, rate, " (stalled)" if stressed else "")


def directed_values(rng, shape):
    values = rng.integers(-128, 128, size=shape, dtype=np.int8)
    # Include signed extremes, zero points, and values near arithmetic ties.
    values.flat[:min(values.size, 8)] = np.array([-128, 127, -1, 0, 1, 23, 4, 64],
                                                dtype=np.int8)[:min(values.size, 8)]
    return values


async def add_case(dut, layer, nrows, rng, stalls, stressed):
    chunk = replace(layer, h_in=1, w_in=nrows, c_in=8, h_out=1, w_out=nrows, c_out=8)
    aa = directed_values(rng, (1, nrows, 8))
    bb = directed_values(rng, aa.shape)
    arows, brows = packed_rows(aa), packed_rows(bb)
    beats = [(a, b, 1, 1, index) for index, (a, b) in enumerate(zip(arows, brows))]
    await run_block(dut, f"ADD-{nrows}", config(chunk, nrows_i=nrows), arows, beats,
                    packed_rows(add(aa, bb, chunk)), stalls, brows, stressed)


async def max_case(dut, layer, tensor, oh0, rows, stalls, stressed):
    groups = layer.c_in // 8
    lo = max(0, oh0 * layer.stride - layer.pad_t)
    hi = min(layer.h_in - 1, (oh0 + rows - 1) * layer.stride - layer.pad_t + layer.kh - 1)
    arows = packed_rows(tensor[lo:hi + 1])
    beats = []
    for oh in range(oh0, oh0 + rows):
        for ow in range(layer.w_out):
            for group in range(groups):
                for kr in range(layer.kh):
                    for kc in range(layer.kw):
                        ih = oh * layer.stride + kr - layer.pad_t
                        iw = ow * layer.stride + kc - layer.pad_l
                        address = None
                        data = pack(np.full(8, layer.pad_value, dtype=np.int8))
                        if 0 <= ih < layer.h_in and 0 <= iw < layer.w_in:
                            address = (ih - lo) * layer.w_in * groups + iw * groups + group
                            data = pack(tensor[ih, iw, group * 8:group * 8 + 8])
                        beats.append((data, 0, int(kr == 0 and kc == 0),
                                      int(kr == layer.kh - 1 and kc == layer.kw - 1), address))
    expected = packed_rows(maxpool(tensor, layer)[oh0:oh0 + rows])
    await run_block(dut, f"MAX-{layer.kh}x{layer.kw}-oh{oh0}",
                    config(layer, oh0_i=oh0, rows_i=rows, lo_i=lo),
                    arows, beats, expected, stalls, stressed=stressed)


async def avg_case(dut, layer, groups, rng, stalls, stressed):
    chunk = replace(layer, c_in=groups * 8, c_out=groups * 8)
    tensor = directed_values(rng, (chunk.h_in, chunk.w_in, chunk.c_in))
    # Directed all-negative/positive lanes also check reduction state restart.
    tensor[:, :, 0] = -128
    tensor[:, :, 1] = 127
    arows = packed_rows(tensor)
    npix = chunk.h_in * chunk.w_in
    beats = [(arows[pix * groups + group], 0, int(pix == 0), int(pix == npix - 1),
              pix * groups + group) for group in range(groups) for pix in range(npix)]
    await run_block(dut, f"AVG-{npix}-ng{groups}", config(chunk), arows, beats,
                    packed_rows(avgpool(tensor, chunk)), stalls, stressed=stressed)


@cocotb.test()
async def vector_blocks(dut):
    rng = np.random.default_rng(0xA601)
    stalls = random.Random(0xBAC4)
    for name in ("clk", "rst_ni", "start_i", "in_wr_en_i", "w_wr_en_i", "out_ready_i",
                 "in_wr_addr_i", "in_wr_data_i", "w_wr_addr_i", "w_wr_data_i"):
        getattr(dut, name).value = 0
    resnet = load_model("resnet50")
    add_layer = next(layer for layer in resnet.layers if layer.kind == "ADD")
    max_layer = replace(next(layer for layer in resnet.layers if layer.kind == "MAXPOOL"),
                        c_in=16, c_out=16)
    avg_layer = next(layer for layer in load_model("efficientnet-lite0").layers
                     if layer.kind == "AVGPOOL")
    for signal, value in config(add_layer, npix_i=1).items():
        handle = getattr(dut, signal)
        handle.value = int(value) & ((1 << len(handle)) - 1)
    await reset(dut)
    for nrows, stressed in [(2048, False), (2048, True), (37, True), (1, False), (1025, True)]:
        await add_case(dut, add_layer, nrows, rng, stalls, stressed)
    tensor = directed_values(rng, (112, 112, 16))
    for oh0, rows, stressed in [(0, 3, False), (23, 4, True), (53, 3, True)]:
        await max_case(dut, max_layer, tensor, oh0, rows, stalls, stressed)
    # Small directed geometry reaches bottom/right padding absent in the real
    # stride-two even input, singleton windows, and non-square tap loops.
    for kh, kw, stride, pad, height, width, groups in [
            (1, 1, 1, 0, 3, 4, 1), (2, 4, 3, 2, 6, 7, 3),
            (3, 3, 1, 2, 4, 5, 2), (1, 1, 2, 3, 2, 2, 1)]:
        ho = (height + 2 * pad - kh) // stride + 1
        wo = (width + 2 * pad - kw) // stride + 1
        layer = replace(max_layer, h_in=height, w_in=width, c_in=groups * 8,
                        c_out=groups * 8, h_out=ho, w_out=wo, kh=kh, kw=kw,
                        stride=stride, pad_t=pad, pad_l=pad, pad_b=pad, pad_r=pad,
                        pad_value=-11, act_min=-87, act_max=101)
        tensor = directed_values(rng, (height, width, groups * 8))
        await max_case(dut, layer, tensor, 0, ho, stalls, True)
    for groups in range(4, 9):
        await avg_case(dut, avg_layer, groups, rng, stalls, groups % 2 == 0)
    singleton = replace(avg_layer, h_in=1, w_in=1, kh=1, kw=1,
                        scalar0=mult_word(*mant_shift(0.75)), scalar1=-avg_layer.zp_in)
    await avg_case(dut, singleton, 1, rng, stalls, False)
    # Reset with outstanding reads and blocked vector results, then prove that
    # neither stale prefetch entries nor reduction state contaminate a restart.
    cfg = config(add_layer, nrows_i=2048, npix_i=1)
    for signal, value in cfg.items():
        handle = getattr(dut, signal)
        handle.value = int(value) & ((1 << len(handle)) - 1)
    dut.out_ready_i.value = 0
    dut.start_i.value = 1
    await tick(dut)
    dut.start_i.value = 0
    for _ in range(25):
        await tick(dut)
    assert int(dut.busy_o.value)
    await reset(dut)
    await add_case(dut, add_layer, 7, rng, stalls, False)


def test_vector_agu():
    run_cocotb("vec_block_tb", ["rtl/vector_agu.sv", "rtl/vector_unit.sv",
                               "rtl/round_shift.sv", "rtl/ram_2p.sv",
                               "tb/unit/vec_block_tb.sv"],
                "tb.unit.test_vector_agu", build_name=Path(__file__).stem)
