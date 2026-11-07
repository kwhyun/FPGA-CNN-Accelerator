"""Bit-exact requant arithmetic, elastic flow, and reset coverage."""
from collections import deque
from pathlib import Path

import cocotb
import numpy as np
from cocotb.triggers import Timer

from tb.common.sim import run_cocotb
from compiler.golden.layers import round_shift
from compiler.model.ir import mult_word


def pack(values, width):
    return sum((int(value) & ((1 << width) - 1)) << (width * lane)
               for lane, value in enumerate(values))


def make_row(acc, bias, mantissa, shift, config):
    zp, low, high = config
    expected = np.clip(round_shift((np.asarray(acc, dtype=np.int64) + bias) * mantissa,
                                  shift) + zp, low, high)
    coef = [(mult_word(int(m), int(s)) << 32) | (int(b) & 0xFFFFFFFF)
            for b, m, s in zip(bias, mantissa, shift)]
    return pack(acc, 32), pack(coef, 64), pack(expected, 8)


async def tick(dut):
    dut.clk.value = 0
    await Timer(5, unit="ns")
    snapshot = (int(dut.in_ready_o.value), int(dut.out_valid_o.value),
                int(dut.out_data_o.value))
    dut.clk.value = 1
    await Timer(5, unit="ns")
    return snapshot


async def reset(dut):
    dut.rst_ni.value = 0
    dut.in_valid_i.value = 0
    dut.out_ready_i.value = 0
    for _ in range(3):
        ready, valid, _ = await tick(dut)
        assert not ready and not valid
    dut.rst_ni.value = 1


async def stream(dut, rows, config, rng, stressed=True):
    dut.zp_out_i.value, dut.act_min_i.value, dut.act_max_i.value = [v & 255 for v in config]
    expected = deque()
    index = 0
    active = False
    stalled = None
    received = 0
    input_cycles = []
    output_cycles = []
    for cycle in range(20000):
        if index < len(rows) and not active:
            active = not stressed or rng.random() < 0.8
        dut.in_valid_i.value = int(active)
        if active:
            dut.in_acc_i.value = rows[index][0]
            dut.in_coef_i.value = rows[index][1]
        ready = (not stressed or
                 (not (25 <= cycle % 240 < 80) and rng.random() < 0.68))
        dut.out_ready_i.value = int(ready)
        in_ready, valid, data = await tick(dut)
        if stalled is not None:
            assert valid and data == stalled, "output changed while stalled"
        stalled = data if valid and not ready else None
        if active and in_ready:
            expected.append((rows[index][2], cycle))
            input_cycles.append(cycle)
            index += 1
            active = False
        if valid and ready:
            assert expected, "unexpected/duplicate output"
            reference, accepted = expected.popleft()
            assert data == reference, f"row {received}: {data:016x} != {reference:016x}"
            if not stressed:
                assert cycle - accepted == 8, (cycle, accepted)
            received += 1
            output_cycles.append(cycle)
        if index == len(rows) and received == len(rows):
            break
    else:
        raise AssertionError(f"stream timeout: sent {index}, received {received}")
    if not stressed:
        assert all(b - a == 1 for a, b in zip(input_cycles, input_cycles[1:]))
        assert all(b - a == 1 for a, b in zip(output_cycles, output_cycles[1:]))
    dut.in_valid_i.value = 0
    dut.out_ready_i.value = 1
    for _ in range(12):
        _, valid, _ = await tick(dut)
        assert not valid, "extra output after draining"


@cocotb.test()
async def requant_bit_exact(dut):
    rng = np.random.default_rng(0x52E0)
    dut.in_acc_i.value = 0
    dut.in_coef_i.value = 0
    dut.zp_out_i.value = 0
    dut.act_min_i.value = 128
    dut.act_max_i.value = 127
    await reset(dut)
    for config in [(0, -128, 127), (-19, -19, 127), (13, 13, 51), (127, -128, 127)]:
        vectors = []
        # Visible positive/negative half ties, and the maximum 33-bit magnitude.
        for shift in [17, 18, 20, 24, 32, 48, 49]:
            half_sum = 1 << (shift - 17)
            for sign in [-1, 1]:
                total = sign * half_sum
                if total > (1 << 32) - 2:
                    continue
                acc = max(-(1 << 31), min((1 << 31) - 1, total))
                bias = total - acc
                vectors.append(make_row([acc] * 8, np.full(8, bias),
                                        np.full(8, 65536), np.full(8, shift), config))
        limits = np.array([-(1 << 31), (1 << 31) - 1, -1, 0, 1, 127, -128, 65537])
        for shift in [1, 16, 17, 31, 48, 63]:
            for mantissa in [65536, 131071]:
                for bias in [limits, limits[::-1], np.zeros(8, dtype=np.int64)]:
                    vectors.append(make_row(limits, bias, np.full(8, mantissa),
                                            np.full(8, shift), config))
        for _ in range(450):
            # Mix large sums and unsaturated results at realistic scale ratios.
            acc = rng.integers(-(1 << 31), 1 << 31, size=8, dtype=np.int64)
            bias = rng.integers(-(1 << 31), 1 << 31, size=8, dtype=np.int64)
            if rng.random() < 0.5:
                acc = rng.integers(-10000, 10001, size=8)
                bias = rng.integers(-10000, 10001, size=8)
            mantissa = rng.integers(65536, 131072, size=8)
            shift = rng.integers(1, 64, size=8)
            vectors.append(make_row(acc, bias, mantissa, shift, config))
        await stream(dut, vectors, config, rng)
    # Full-rate flow proves the documented eight-cycle latency and II=1.
    rows = [make_row([value] * 8, np.zeros(8, dtype=np.int64), np.full(8, 65536),
                     np.full(8, 17), (0, -128, 127)) for value in range(-100, 101)]
    await stream(dut, rows, (0, -128, 127), rng, stressed=False)
    # Reset discards both occupied skid slots and all in-flight pipeline entries.
    dut.out_ready_i.value = 0
    dut.in_valid_i.value = 1
    for _ in range(18):
        await tick(dut)
    await reset(dut)
    await stream(dut, rows[:30], (0, -128, 127), rng, stressed=False)


def test_quantization():
    run_cocotb("quantization", ["rtl/round_shift.sv", "rtl/quantization_lane.sv", "rtl/quantization.sv"],
               "tb.unit.test_quantization", build_name=Path(__file__).stem)
