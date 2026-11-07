"""Cycle-accurate WS array arithmetic, bank lifetime, and stream tests."""
from __future__ import annotations

from collections import deque

import cocotb
import numpy as np
from cocotb.triggers import Timer

from tb.common.sim import run_cocotb
from compiler.golden.layers import _exact_matmul

DIM = 8
LATENCY = 2 * DIM
TAG_W = 23
TAG_MASK = (1 << TAG_W) - 1
ROW_RELEASE = DIM - 1  # row k shadow free after edge swap + k + ROW_RELEASE


def _pack(lanes, width: int) -> int:
    mask = (1 << width) - 1
    return sum((int(value) & mask) << (width * lane) for lane, value in enumerate(lanes))


def _unpack_result(word: int) -> list[int]:
    lanes = [(word >> (32 * lane)) & 0xFFFFFFFF for lane in range(DIM)]
    return [value - (1 << 32) if value & (1 << 31) else value for value in lanes]


class ArrayScoreboard:
    """Independent accepted-beat model, with an expected result for each edge."""

    def __init__(self, dut):
        self.dut = dut
        self.cycle = -1
        self.shadow = np.zeros((DIM, DIM), dtype=np.int8)
        self.active = None
        self.load_count = 0
        self.full = False
        self.release_at = [None] * DIM
        self.expected = {}
        self.tags = deque()
        self.serial = 0
        self.outputs = 0
        self.accepted_weights = 0

    async def reset(self):
        self.dut.clk.value = 0
        self.dut.rst_ni.value = 0
        self.dut.w_valid_i.value = 0
        self.dut.w_row_i.value = 0
        self.dut.a_valid_i.value = 0
        self.dut.a_row_i.value = 0
        self.dut.a_swap_i.value = 0
        self.dut.a_tag_i.value = 0
        for _ in range(3):
            await Timer(5, unit="ns")
            self.dut.clk.value = 1
            await Timer(5, unit="ns")
            assert int(self.dut.c_valid_o.value) == 0
            self.dut.clk.value = 0
        self.dut.rst_ni.value = 1
        await Timer(1, unit="ns")

    def check_control(self):
        ready = not self.full and self.release_at[self.load_count] is None
        assert int(self.dut.w_ready_o.value) == ready, f"w_ready at edge {self.cycle}"
        assert int(self.dut.shadow_full_o.value) == self.full, (
            f"shadow_full at edge {self.cycle}, accepted beats={self.load_count}"
        )
        return ready

    async def step(self, *, row=None, swap=False, weight=None) -> bool:
        ready = self.check_control()
        was_full = self.full
        self.cycle += 1
        dut = self.dut
        dut.clk.value = 0
        dut.w_valid_i.value = int(weight is not None)
        dut.w_row_i.value = _pack(weight, 8) if weight is not None else 0xDEADBEEF12345678
        dut.a_valid_i.value = int(row is not None)
        dut.a_row_i.value = _pack(row, 8) if row is not None else 0x80FF017F55AA33CC
        # Deliberately allow swap=1 on invalid bubbles: it must have no effect.
        dut.a_swap_i.value = int(swap)
        tag = (0x513827 + self.serial * 0x127) & TAG_MASK
        dut.a_tag_i.value = tag

        accepted = weight is not None and ready
        if accepted:
            self.shadow[self.load_count] = weight
            self.load_count += 1
            self.accepted_weights += 1
            if self.load_count == DIM:
                self.full = True
                self.load_count = 0

        if row is not None:
            if swap:
                assert was_full, "test attempted a swap before eight accepted weight beats"
                assert all(r is None for r in self.release_at)
                self.active = self.shadow.copy()
                self.full = False
                self.release_at = [self.cycle + k + ROW_RELEASE for k in range(DIM)]
            assert self.active is not None, "first row must select initialized weights"
            # Reuse the integer golden kernel and independently check numpy's
            # INT64 matmul (no quantization or rounding applies to this module).
            activation = np.asarray(row, dtype=np.int8).reshape(1, DIM)
            result = _exact_matmul(activation, self.active)[0]
            np.testing.assert_array_equal(
                result, (activation.astype(np.int64) @ self.active.astype(np.int64))[0]
            )
            self.expected[self.cycle + LATENCY] = (tag, result.tolist())
            self.tags.append(tag)
            self.serial += 1

        self.release_at = [None if r == self.cycle else r for r in self.release_at]

        await Timer(5, unit="ns")
        dut.clk.value = 1
        await Timer(5, unit="ns")
        expected = self.expected.pop(self.cycle, None)
        assert int(dut.c_valid_o.value) == (expected is not None), (
            f"valid/latency mismatch at edge {self.cycle}, expected {expected}"
        )
        if expected is not None:
            expected_tag, expected_lanes = expected
            actual_tag = int(dut.c_tag_o.value)
            assert actual_tag == expected_tag == self.tags.popleft(), (
                f"tag mismatch at edge {self.cycle}"
            )
            actual_lanes = _unpack_result(int(dut.c_row_o.value))
            assert actual_lanes == expected_lanes, (
                f"edge {self.cycle}, tag {actual_tag}: {actual_lanes} != {expected_lanes}"
            )
            self.outputs += 1
        self.check_control()
        return accepted

    async def preload(self, weights, rng=None):
        for weight in weights:
            if rng is not None:
                for _ in range(int(rng.integers(0, 4))):
                    await self.step(swap=True)
            # The source holds this exact beat until accepted, including when
            # the preceding tile's swap is still moving through the array.
            for _ in range(64):
                if await self.step(weight=weight):
                    break
            else:
                raise AssertionError("shadow preload did not become ready")
        assert self.full

    async def finish(self):
        for _ in range(LATENCY + 3):
            await self.step(swap=True)
        assert not self.expected
        assert not self.tags
        assert self.outputs == self.serial


@cocotb.test()
async def single_tile_many_rows(dut):
    rng = np.random.default_rng(0xA771)
    score = ArrayScoreboard(dut)
    await score.reset()
    weights = rng.integers(-128, 128, (DIM, DIM), dtype=np.int16).astype(np.int8)
    await score.preload(weights, rng)
    # A complete shadow tile must stay full until a valid swap row arrives.
    for _ in range(4):
        await score.step(swap=True)
    directed = [np.zeros(DIM, dtype=np.int8), np.ones(DIM, dtype=np.int8)]
    directed += [np.eye(DIM, dtype=np.int8)[lane] * 127 for lane in range(DIM)]
    directed += [np.eye(DIM, dtype=np.int8)[lane] * -128 for lane in range(DIM)]
    random_rows = rng.integers(-128, 128, (320, DIM), dtype=np.int16).astype(np.int8)
    for index, row in enumerate([*directed, *random_rows]):
        if rng.random() < 0.25:
            await score.step(swap=True)
        await score.step(row=row, swap=index == 0)
    await score.finish()


@cocotb.test()
async def overlapping_tiles_with_bubbles(dut):
    rng = np.random.default_rng(0xBA6C)
    score = ArrayScoreboard(dut)
    await score.reset()
    weights = rng.integers(-128, 128, (9, DIM, DIM), dtype=np.int16).astype(np.int8)
    await score.preload(weights[0], rng)
    for tile in range(len(weights)):
        rows = rng.integers(-128, 128, (80 + tile, DIM), dtype=np.int16).astype(np.int8)
        row_index = 0
        next_beat = 0
        pending = False
        preload_overlap = 0
        has_next = tile + 1 < len(weights)
        # Successor preloads begin immediately, hold valid through the retiring
        # bank's busy interval, then insert random gaps between accepted beats.
        while row_index < len(rows) or (has_next and next_beat < DIM):
            send_row = row_index < len(rows) and rng.random() < 0.74
            if has_next and next_beat < DIM and (pending or rng.random() < 0.67):
                pending = True
                weight = weights[tile + 1, next_beat]
            else:
                weight = None
            accepted = await score.step(
                row=rows[row_index] if send_row else None,
                swap=(row_index == 0) if send_row else True,
                weight=weight,
            )
            if accepted:
                assert has_next
                preload_overlap += int(row_index < len(rows))
                next_beat += 1
                pending = False
            row_index += int(send_row)
        if has_next:
            assert preload_overlap == DIM, "the next preload must overlap current streaming"
    await score.finish()


@cocotb.test()
async def one_row_tiles_minimum_spacing(dut):
    rng = np.random.default_rng(0x1123)
    score = ArrayScoreboard(dut)
    await score.reset()
    weights = rng.integers(-128, 128, (14, DIM, DIM), dtype=np.int16).astype(np.int8)
    rows = rng.integers(-128, 128, (len(weights), DIM), dtype=np.int16).astype(np.int8)
    await score.preload(weights[0])
    last_swap = None
    for tile, row in enumerate(rows):
        # Offer the first next-tile beat on the swap edge, when it must stall.
        has_next = tile + 1 < len(weights)
        weight = weights[tile + 1, 0] if has_next else None
        assert not await score.step(row=row, swap=True, weight=weight)
        if last_swap is not None and tile <= 7:
            assert score.cycle - last_swap == 2 * DIM
        last_swap = score.cycle
        if has_next:
            assert await score.step(weight=weight) is False
            # Keep the first stalled beat stable; only later beats may have gaps.
            for beat in range(DIM):
                if tile >= 7 and beat > 0:
                    for _ in range(int(rng.integers(0, 4))):
                        await score.step(swap=True)
                for _ in range(64):
                    if await score.step(weight=weights[tile + 1, beat]):
                        break
                else:
                    raise AssertionError("one-row tile preload timed out")
            assert score.full
            if tile < 7:
                assert score.cycle - last_swap == ROW_RELEASE + DIM
    assert score.accepted_weights == len(weights) * DIM
    await score.finish()


@cocotb.test()
async def extreme_signed_values(dut):
    rng = np.random.default_rng(0x807F)
    score = ArrayScoreboard(dut)
    await score.reset()
    checker = np.where(np.indices((DIM, DIM)).sum(axis=0) % 2, -128, 127).astype(np.int8)
    weights = [
        np.full((DIM, DIM), -128, dtype=np.int8),
        np.full((DIM, DIM), 127, dtype=np.int8),
        checker,
        checker.T[::-1].copy(),
        np.eye(DIM, dtype=np.int8) * -128,
    ]
    all_patterns = np.array(
        [[-128 if mask & (1 << lane) else 127 for lane in range(DIM)] for mask in range(256)],
        dtype=np.int8,
    )
    for tile_weights in weights:
        await score.preload(tile_weights, rng)
        for index, row in enumerate(all_patterns):
            await score.step(row=row, swap=index == 0)
    await score.finish()


@cocotb.test()
async def reset_during_swap_and_partial_preload(dut):
    rng = np.random.default_rng(0x5E7)
    score = ArrayScoreboard(dut)
    await score.reset()
    weights = rng.integers(-128, 128, (DIM, DIM), dtype=np.int16).astype(np.int8)
    await score.preload(weights)
    for index in range(4):
        await score.step(row=weights[index], swap=index == 0)
    # Reset while PE banks differ and output rows are still in flight.
    score = ArrayScoreboard(dut)
    await score.reset()
    for row in weights[:3]:
        assert await score.step(weight=row)
    # Also abandon a partial shadow tile; the next beat must become row zero.
    score = ArrayScoreboard(dut)
    await score.reset()
    new_weights = rng.integers(-128, 128, (DIM, DIM), dtype=np.int16).astype(np.int8)
    await score.preload(new_weights)
    for index, row in enumerate(weights):
        await score.step(row=row, swap=index == 0)
    await score.finish()


def test_ws_array():
    run_cocotb(
        "ws_array",
        ["rtl/ws_pe.sv", "rtl/ws_array.sv"],
        "tb.unit.test_ws_array",
        parameters={"TAG_W": TAG_W},
        build_name="test_ws_array",
    )
