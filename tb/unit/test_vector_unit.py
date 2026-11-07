"""Vector arithmetic, reduction grouping, stalls, and real ResNet50 scales."""
from collections import deque
from dataclasses import replace
from pathlib import Path

import cocotb
import numpy as np
from cocotb.triggers import Timer

from tb.common.sim import run_cocotb
from compiler.golden.layers import add, avgpool, round_shift
from compiler.model.ir import load_model, mult_word, word_ms


def pack(values):
    return sum((int(value) & 255) << (8 * lane) for lane, value in enumerate(values))


def configuration(op, ma=65536, shift=17, mb_or_bias=65536,
                  za=0, zb=0, zo=0, low=-128, high=127):
    return dict(op_i=op, zp_a_i=za & 255, zp_b_i=zb & 255, zp_out_i=zo & 255,
                act_min_i=low & 255, act_max_i=high & 255,
                scalar0_i=mult_word(ma, shift), scalar1_i=mb_or_bias & 0xFFFFFFFF)


def signed(value, width=8):
    return value - (1 << width) if value & (1 << (width - 1)) else value


def model(a, b, config):
    op = config['op_i']
    ma, shift = word_ms(config['scalar0_i'])
    if op == 0:
        value = ((a.astype(np.int64) - signed(config['zp_a_i'])) * ma +
                 (b.astype(np.int64) - signed(config['zp_b_i'])) *
                 (config['scalar1_i'] & 0x1FFFF))
        value = round_shift(value, shift) + signed(config['zp_out_i'])
    elif op == 1:
        value = a.max(axis=0)
    else:
        total = a.astype(np.int64).sum(axis=0) + signed(config['scalar1_i'], 32)
        value = round_shift(total * ma, shift) + signed(config['zp_out_i'])
    return np.clip(value, signed(config['act_min_i']), signed(config['act_max_i']))


def add_beats(a, b, config, expected=None):
    if expected is None:
        expected = model(a, b, config)
    # ADD ignores grouping flags, including all four combinations.
    return [(pack(aa), pack(bb), index % 2, (index // 2) % 2, pack(yy))
            for index, (aa, bb, yy) in enumerate(zip(a, b, expected))]


def group_beats(groups, config, expected=None):
    beats = []
    for index, values in enumerate(groups):
        result = model(values, None, config) if expected is None else expected[index]
        beats.extend((pack(row), 0, int(beat == 0), int(beat == len(values) - 1),
                      pack(result) if beat == len(values) - 1 else None)
                     for beat, row in enumerate(values))
    return beats


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


async def stream(dut, beats, config, rng, stressed=True):
    for name, value in config.items():
        getattr(dut, name).value = value
    pending = deque()
    index = 0
    received = 0
    target = sum(beat[4] is not None for beat in beats)
    active = False
    stalled = None
    input_cycles = []
    for cycle in range(50000):
        if index < len(beats) and not active:
            active = not stressed or rng.random() < 0.8
        dut.in_valid_i.value = int(active)
        if active:
            aa, bb, first, last, result = beats[index]
            dut.in_a_i.value = aa
            dut.in_b_i.value = bb
            dut.in_first_i.value = first
            dut.in_last_i.value = last
        ready = (not stressed or
                 (not (30 <= cycle % 230 < 85) and rng.random() < 0.65))
        dut.out_ready_i.value = int(ready)
        in_ready, valid, data = await tick(dut)
        if stalled is not None:
            assert valid and data == stalled, "output changed while stalled"
        stalled = data if valid and not ready else None
        if active and in_ready:
            if result is not None:
                pending.append((result, cycle))
            input_cycles.append(cycle)
            index += 1
            active = False
        if valid and ready:
            assert pending, "unexpected output (non-last, duplicate, or reset residue)"
            reference, accepted = pending.popleft()
            assert data == reference, (f"op {config['op_i']}, result {received}: "
                                       f"{data:016x} != {reference:016x}")
            if not stressed:
                assert cycle - accepted == 9, (cycle, accepted)
            received += 1
        if index == len(beats) and received == target:
            break
    else:
        raise AssertionError(f"stream timeout: sent {index}, received {received}/{target}")
    if not stressed:
        assert all(b - a == 1 for a, b in zip(input_cycles, input_cycles[1:]))
    dut.in_valid_i.value = 0
    dut.out_ready_i.value = 1
    for _ in range(13):
        _, valid, _ = await tick(dut)
        assert not valid, "extra output after draining"


@cocotb.test()
async def vector_bit_exact(dut):
    rng = np.random.default_rng(0xADD7020)
    for name, value in configuration(0).items():
        getattr(dut, name).value = value
    dut.in_a_i.value = 0
    dut.in_b_i.value = 0
    dut.in_first_i.value = 0
    dut.in_last_i.value = 0
    await reset(dut)

    # Shared shifts, both mantissa limits, zero smaller mantissa, signed 9-bit
    # differences at +/-255, half ties, zero-point addition, and activation clamps.
    configs = [configuration(0, ma, shift, mb, za, zb, zo, low, high)
               for ma, shift, mb, za, zb, zo, low, high in [
                   (65536, 17, 65536, 0, 0, 0, -128, 127),
                   (65536, 18, 0, 0, 0, 0, -128, 127),
                   (65536, 20, 65536, 0, 0, -7, -128, 127),
                   (131071, 1, 131071, -128, 127, 127, -128, 127),
                   (65536, 63, 131071, 127, -128, -43, -128, 127),
                   (131071, 19, 0, -128, 127, 13, 13, 55),
               ]]
    for _ in range(15):
        low = int(rng.integers(-128, 40))
        configs.append(configuration(0, int(rng.integers(0, 131072)),
                                     int(rng.integers(1, 64)),
                                     int(rng.integers(0, 131072)),
                                     *[int(v) for v in rng.integers(-128, 128, 3)],
                                     low, int(rng.integers(max(low, 0), 128))))
    directed = np.array([[-128, 127, -1, 0, 1, -2, 2, 3],
                         [1, -1, 2, -2, 8, -8, 16, -16]], dtype=np.int8)
    for config in configs:
        a = np.concatenate([directed, rng.integers(-128, 128, (80, 8), dtype=np.int8)])
        b = np.concatenate([directed[:, ::-1],
                            rng.integers(-128, 128, (80, 8), dtype=np.int8)])
        await stream(dut, add_beats(a, b, config), config, rng)

    # MAX: every legal group length, all-negative windows, singleton groups,
    # consecutive groups, first/last extrema, and clamping independent of zps.
    for low, high in [(-128, 127), (-23, 42), (20, 20)]:
        config = configuration(1, za=-128, zb=127, zo=91, low=low, high=high)
        groups = [rng.integers(-128, 128, (size, 8), dtype=np.int8)
                  for size in range(1, 50)]
        groups += [np.full((49, 8), -128, dtype=np.int8),
                   np.arange(-80, -8, dtype=np.int8).reshape(9, 8)]
        await stream(dut, group_beats(groups, config), config, rng)

    # AVG: the signed high/unsigned low split must handle borrow, sign changes,
    # INT32 bias extremes and 33-bit bias sums. All sums stay inside INT32.
    configs = [configuration(2, ma, shift, bias, zo=zo, low=low, high=high)
               for ma, shift, bias, zo, low, high in [
                   (65536, 17, 0, 0, -128, 127),
                   (65536, 18, 0, 0, -128, 127),
                   (65536, 20, 0, 0, -128, 127),
                   (65536, 1, -(1 << 31), 0, -128, 127),
                   (131071, 1, (1 << 31) - 1, 0, -128, 127),
                   (65536, 48, -(1 << 31), 0, -128, 127),
                   (131071, 63, (1 << 31) - 1, -59, -128, 127),
                   (131071, 27, 131071, 5, 5, 52),
               ]]
    for _ in range(12):
        configs.append(configuration(2, int(rng.integers(65536, 131072)),
                                     int(rng.integers(1, 64)),
                                     int(rng.integers(-(1 << 31), 1 << 31)),
                                     zo=int(rng.integers(-128, 128))))
    for config in configs:
        groups = [directed[:1], directed[1:], np.zeros((1, 8), dtype=np.int8),
                  np.full((49, 8), -128, dtype=np.int8),
                  np.full((49, 8), 127, dtype=np.int8)]
        groups += [rng.integers(-128, 128, (int(size), 8), dtype=np.int8)
                   for size in rng.integers(1, 50, size=8)]
        await stream(dut, group_beats(groups, config), config, rng)

    # Real layers: retain all quantization settings, crop spatial ADD dimensions
    # and channels; AVG retains the real 7x7 window and its exact scalar0/scalar1.
    network = load_model('resnet50')
    real_add = next(layer for layer in network.layers if layer.kind == 'ADD')
    layer = replace(real_add, h_in=2, w_in=3, h_out=2, w_out=3, c_in=24, c_out=24)
    a = rng.integers(-128, 128, (2, 3, 24), dtype=np.int8)
    b = rng.integers(-128, 128, a.shape, dtype=np.int8)
    config = configuration(0, *word_ms(layer.scalar0), layer.scalar1,
                           layer.zp_in, layer.zp_in2, layer.zp_out,
                           layer.act_min, layer.act_max)
    await stream(dut, add_beats(a.reshape(-1, 8), b.reshape(-1, 8), config,
                                add(a, b, layer).reshape(-1, 8)), config, rng)
    real_avg = next(layer for layer in network.layers if layer.kind == 'AVGPOOL')
    assert (real_avg.h_in, real_avg.w_in) == (7, 7)
    layer = replace(real_avg, c_in=24, c_out=24)
    a = rng.integers(-128, 128, (7, 7, 24), dtype=np.int8)
    config = configuration(2, *word_ms(layer.scalar0), layer.scalar1,
                           layer.zp_in, layer.zp_in2, layer.zp_out,
                           layer.act_min, layer.act_max)
    groups = [a[:, :, offset:offset + 8].reshape(49, 8) for offset in range(0, 24, 8)]
    await stream(dut, group_beats(groups, config, avgpool(a, layer).reshape(-1, 8)),
                 config, rng)

    # Continuous traffic checks II=1 and nine-cycle latency, including 49-row
    # reductions and adjacent singleton windows under each operation.
    for op in range(3):
        config = configuration(op, mb_or_bias=65536 if op == 0 else 0)
        if op == 0:
            a = rng.integers(-128, 128, (100, 8), dtype=np.int8)
            beats = add_beats(a, a, config)
        else:
            groups = [rng.integers(-128, 128, (size, 8), dtype=np.int8)
                      for size in [1] * 25 + [49, 2, 49, 1]]
            beats = group_beats(groups, config)
        await stream(dut, beats, config, rng, stressed=False)

    # Flush a full output buffer and pipeline, then restart with fresh groups.
    config = configuration(0)
    for name, value in config.items():
        getattr(dut, name).value = value
    dut.in_valid_i.value = 1
    dut.out_ready_i.value = 0
    for _ in range(20):
        await tick(dut)
    await reset(dut)
    for op in [1, 2]:
        config = configuration(op, mb_or_bias=0)
        for name, value in config.items():
            getattr(dut, name).value = value
        # A partial pre-reset group must not contaminate the next first beat.
        dut.in_valid_i.value = 1
        dut.in_first_i.value = 1
        dut.in_last_i.value = 0
        dut.in_a_i.value = pack([127] * 8)
        await tick(dut)
        await reset(dut)
        groups = [np.full((2, 8), -31, dtype=np.int8), directed[:1]]
        await stream(dut, group_beats(groups, config), config, rng, stressed=False)


def test_vector_unit():
    run_cocotb("vector_unit", ["rtl/round_shift.sv", "rtl/vector_unit.sv"],
               "tb.unit.test_vector_unit", build_name=Path(__file__).stem)
