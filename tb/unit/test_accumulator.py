"""Bit-exact accumulator tests, including forwarding and drain credit hazards."""
from __future__ import annotations

import os
import random
from collections import deque
from pathlib import Path

import cocotb
import numpy as np
import pytest
from cocotb.triggers import Timer

from tb.common.sim import VERILATOR_ARGS, run_cocotb
from compiler.golden.layers import _exact_matmul
from compiler.model.ir import INT32_MAX, INT32_MIN


def pack(row):
    return sum((int(value) & 0xFFFFFFFF) << (32 * lane) for lane, value in enumerate(row))


def wrap32(value):
    return (int(value) - INT32_MIN) % (1 << 32) + INT32_MIN


class AccumulatorBench:
    def __init__(self, dut):
        self.dut = dut
        self.depth = int(os.environ["ACCUMULATOR_TEST_DEPTH"])
        self.lanes = len(dut.wr_data_i) // 32
        self.rng = random.Random(0xACC0520)
        self.rows = [{}, {}]
        self.expected = deque()
        self.cycle = 0
        self.previous_write = False
        self.stalled_response = None
        self.stalled_request = None
        self.accepted = 0
        self.received = 0

    def random_row(self):
        # Full-width signed values also exercise independent lane wraparound.
        return tuple(self.rng.randint(INT32_MIN, INT32_MAX) for _ in range(self.lanes))

    async def reset(self):
        dut = self.dut
        dut.rst_ni.value = 0
        dut.wr_valid_i.value = 0
        dut.wr_bank_i.value = 0
        dut.wr_addr_i.value = 0
        dut.wr_accum_i.value = 0
        dut.wr_data_i.value = 0
        dut.rd_valid_i.value = 0
        dut.rd_bank_i.value = 0
        dut.rd_addr_i.value = 0
        dut.rdata_ready_i.value = 0
        for _ in range(2):
            dut.clk.value = 0
            await Timer(5, unit="ns")
            dut.clk.value = 1
            await Timer(5, unit="ns")
            self.cycle += 1
        assert int(dut.busy_o.value) == 0
        assert int(dut.rdata_valid_o.value) == 0
        self.expected.clear()
        self.previous_write = False
        self.stalled_response = None
        self.stalled_request = None
        dut.rst_ni.value = 1

    async def step(self, write=None, request=None, ready=True):
        """Drive on the low phase and score handshakes before the rising edge.

        write = (bank, address, accumulate, signed-lane tuple).
        Requests must remain stable while the DUT deasserts rd_ready_o.
        """
        dut = self.dut
        if self.stalled_request is not None:
            assert request == self.stalled_request, "test driver changed a stalled request"
        dut.clk.value = 0
        dut.wr_valid_i.value = int(write is not None)
        if write is not None:
            bank, address, accumulate, row = write
            dut.wr_bank_i.value = bank
            dut.wr_addr_i.value = address
            dut.wr_accum_i.value = int(accumulate)
            dut.wr_data_i.value = pack(row)
        dut.rd_valid_i.value = int(request is not None)
        if request is not None:
            dut.rd_bank_i.value, dut.rd_addr_i.value = request
        dut.rdata_ready_i.value = int(ready)
        await Timer(5, unit="ns")

        request_ready = bool(dut.rd_ready_o.value)
        response_valid = bool(dut.rdata_valid_o.value)
        response = int(dut.rdata_o.value) if response_valid else None
        if self.stalled_response is not None:
            assert response_valid, "response valid dropped under backpressure"
            assert response == self.stalled_response, "stalled response changed"
        latency = None
        if response_valid:
            assert self.expected, f"unsolicited response at cycle {self.cycle}"
            expected, issued_at, bank, address = self.expected[0]
            assert response == expected, (
                f"cycle {self.cycle}, bank {bank}, row {address}: "
                f"got {response:#x}, expected {expected:#x}"
            )
            assert self.cycle - issued_at >= 2, "response preceded synchronous RAM/FIFO latency"
            if ready:
                self.expected.popleft()
                self.received += 1
                latency = self.cycle - issued_at
        self.stalled_response = response if response_valid and not ready else None

        accepted = request is not None and request_ready
        self.stalled_request = request if request is not None and not accepted else None
        if accepted:
            bank, address = request
            assert write is None or bank != write[0], "illegal same-bank stimulus"
            self.expected.append((pack(self.rows[bank][address]), self.cycle, bank, address))
            self.accepted += 1
        if write is not None:
            bank, address, accumulate, row = write
            if accumulate:
                old = self.rows[bank][address]
                row = tuple(wrap32(left + right) for left, right in zip(old, row))
            self.rows[bank][address] = tuple(row)

        dut.clk.value = 1
        await Timer(5, unit="ns")
        assert bool(dut.busy_o.value) == bool(write is not None or self.previous_write), (
            f"incorrect two-stage busy at cycle {self.cycle}"
        )
        self.previous_write = write is not None
        self.cycle += 1
        return accepted, latency, request_ready

    async def flush(self):
        assert self.stalled_request is None
        for _ in range(32):
            if not self.expected and not bool(self.dut.busy_o.value):
                break
            await self.step()
        else:
            raise AssertionError("write pipeline/drain did not empty")
        # Detect an extra response after the expected queue is empty.
        await self.step()

    async def drain(self, requests, random_stalls=False, full_rate=False):
        index = 0
        response_cycles = []
        for tick in range(20 * len(requests) + 128):
            request = requests[index] if index < len(requests) else None
            ready = not random_stalls or (tick % 79 >= 17 and self.rng.random() < 0.65)
            accepted, latency, request_ready = await self.step(request=request, ready=ready)
            if full_rate and request is not None:
                assert request_ready, "drain failed to accept one request/cycle"
            if accepted:
                index += 1
            if latency is not None:
                response_cycles.append(self.cycle - 1)
                if full_rate:
                    assert latency == 2, f"unstalled drain latency was {latency}, expected 2"
            if index == len(requests) and not self.expected:
                break
        else:
            raise AssertionError("drain deadlocked under backpressure")
        if full_rate:
            assert len(response_cycles) == len(requests)
            assert all(right - left == 1 for left, right in zip(response_cycles, response_cycles[1:]))


@cocotb.test()
async def accumulator_bit_exact(dut):
    bench = AccumulatorBench(dut)
    await bench.reset()
    rng = bench.rng
    lanes = bench.lanes
    depth = bench.depth

    # Initialize every physical row, including address zero and DEPTH-1.
    # Accumulating into unwritten BRAM is intentionally not defined.
    for address in range(depth):
        for bank in range(2):
            await bench.step(write=(bank, address, False, bench.random_row()))
    await bench.flush()

    # Directed overflow, overwrite priority, bank tags and hazard distances.
    extremes = (INT32_MIN, INT32_MAX, -1, 0, 1, 0x40000000, -0x40000000, 37)
    for bank in range(2):
        for distance in (1, 2, 3, 4, 7, 13):
            row = tuple(extremes[lane % len(extremes)] for lane in range(lanes))
            await bench.step(write=(bank, 0, False, row))
            for repeat in range(24):
                for _ in range(distance - 1):
                    # Intervening writes test both address and bank comparisons.
                    filler_bank = bank if depth > 1 else 1 - bank
                    filler_addr = rng.randrange(1, depth) if depth > 1 else 0
                    await bench.step(write=(filler_bank, filler_addr, False, bench.random_row()))
                await bench.step(write=(bank, 0, repeat % 9 != 4, bench.random_row()))
            # No busy wait: the read must see even the most recent write.
            await bench.drain([(bank, 0)])
    await bench.flush()

    # Exact dense FC partial sums from the existing INT8 golden kernel.
    # One address receives an overwrite followed by 63 consecutive accumulates.
    np_rng = np.random.default_rng(0x7020)
    for bank in range(2):
        activation = np_rng.integers(-128, 128, size=(1, 512), dtype=np.int16).astype(np.int8)
        weight = np_rng.integers(-128, 128, size=(512, lanes), dtype=np.int16).astype(np.int8)
        for offset in range(0, 512, 8):
            partial = _exact_matmul(activation[:, offset:offset + 8], weight[offset:offset + 8])[0]
            await bench.step(write=(bank, depth - 1, offset != 0, tuple(map(int, partial))))
        assert bench.rows[bank][depth - 1] == tuple(map(int, _exact_matmul(activation, weight)[0]))
        await bench.drain([(bank, depth - 1)])
    await bench.flush()

    # Port A/B collision forwarding into drain at distance two, with a
    # different-bank write occupying the sum stage and a bubble variant.
    for bubble in (False, True):
        for bank in range(2):
            await bench.step(write=(bank, 0, True, bench.random_row()))
            middle = None if bubble else (1 - bank, 0, True, bench.random_row())
            await bench.step(write=middle)
            await bench.drain([(bank, 0)])
    await bench.flush()

    # Alternate roles immediately every cycle while both write stages are busy.
    # Drain reads the opposite bank's immediately preceding update.
    for tick in range(160):
        bank = tick % 2
        await bench.step(
            write=(bank, 0, tick % 11 != 0, bench.random_row()),
            request=(1 - bank, 0),
        )
    await bench.flush()

    # Fill all four credits, then hold a fifth request and a valid response.
    requests = [(tick % 2, tick % depth) for tick in range(12)]
    index = 0
    for tick in range(30):
        accepted, _, request_ready = await bench.step(request=requests[index], ready=False)
        index += int(accepted)
        if tick >= 4:
            assert not request_ready, "full drain FIFO did not backpressure requests"
    assert index == 4
    await bench.drain(requests[index:])
    await bench.flush()

    # Concurrent writes and lossless drain, then repeated ping-pong bank swaps.
    for write_bank in (0, 1, 0, 1):
        pending = None
        last_address = 0
        for tick in range(900):
            address = last_address if rng.random() < 0.55 else rng.randrange(depth)
            last_address = address
            write = None
            if rng.random() < 0.93:
                write = (write_bank, address, rng.random() < 0.82, bench.random_row())
            if pending is None and rng.random() < 0.9:
                pending = (1 - write_bank, rng.randrange(depth))
            accepted, _, _ = await bench.step(
                write=write, request=pending, ready=tick % 137 >= 31 and rng.random() < 0.68,
            )
            if accepted:
                pending = None
        if pending is not None:
            await bench.drain([pending])
        await bench.flush()

    # Interleave both banks in the write pipeline and in the response FIFO.
    pending = None
    for tick in range(1100):
        if pending is None and rng.random() < 0.88:
            pending = (rng.randrange(2), rng.randrange(depth))
        bank = 1 - pending[0] if pending is not None else rng.randrange(2)
        address = 0 if rng.random() < 0.6 else rng.randrange(depth)
        write = (bank, address, rng.random() < 0.75, bench.random_row())
        accepted, _, _ = await bench.step(
            write=write, request=pending, ready=tick % 97 >= 19 and rng.random() < 0.7,
        )
        if accepted:
            pending = None
    if pending is not None:
        await bench.drain([pending])
    await bench.flush()

    # Check sustained throughput, exact latency and all rows after random RMW.
    requests = [(bank, address) for address in range(depth) for bank in range(2)]
    await bench.drain(requests, full_rate=True)
    await bench.drain(list(reversed(requests)), random_stalls=True)
    await bench.flush()
    assert bench.received == bench.accepted

    # Reset cancels queued/pending responses while preserving committed BRAM.
    for tick in range(4):
        await bench.step(request=(tick % 2, tick % depth), ready=False)
    await bench.reset()
    await bench.drain(requests, full_rate=True)
    await bench.flush()

    # Reset also cancels both write stages, including a pending port A commit.
    original = bench.rows[0][0]
    await bench.step(write=(0, 0, False, bench.random_row()))
    await bench.step(write=(0, 0, True, bench.random_row()))
    await bench.reset()
    bench.rows[0][0] = original
    await bench.drain([(0, 0), (1, 0)], full_rate=True)
    await bench.flush()
    dut._log.info("Checked %d lanes, depth %d, %d clock cycles", lanes, depth, bench.cycle)


@pytest.mark.parametrize(("depth", "data_w"), [(512, 256), (17, 96), (1, 32)])
def test_accumulator(depth, data_w, monkeypatch):
    monkeypatch.setenv("ACCUMULATOR_TEST_DEPTH", str(depth))
    # Enable the bank-conflict assertion for all legal traffic, without editing
    # the shared runner or changing other tests' build arguments.
    monkeypatch.setattr("tb.common.sim.VERILATOR_ARGS", [*VERILATOR_ARGS, "--assert"])
    run_cocotb(
        "accumulator",
        ["rtl/ram_2p.sv", "rtl/accumulator.sv"],
        "tb.unit.test_accumulator",
        parameters={"DEPTH": depth, "DATA_W": data_w},
        build_name=Path(__file__).stem,
    )
