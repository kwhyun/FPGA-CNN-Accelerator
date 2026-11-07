"""Randomized bank ownership, wrap, and simultaneous turnover checks."""
from collections import deque
from pathlib import Path
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from tb.common.sim import run_cocotb


@cocotb.test()
async def ownership(dut):
    Clock(dut.clk, 10, unit="ns").start()
    dut.rst_ni.value = 0
    dut.clear_i.value = 0
    dut.publish_i.value = 0
    dut.release_i.value = 0
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_ni.value = 1
    queue = deque()
    produced = consumed = 0
    rng = random.Random(7020)
    for cycle in range(3000):
        await Timer(1, unit="ns")
        assert int(dut.produce_ready_o.value) == (len(queue) < 2)
        assert int(dut.consume_valid_o.value) == bool(queue)
        assert int(dut.produce_bank_o.value) == (produced & 1)
        assert int(dut.consume_bank_o.value) == (consumed & 1)
        publish = len(queue) < 2 and rng.random() < .7
        release = bool(queue) and rng.random() < .6
        if release:
            assert queue.popleft() == (consumed, consumed & 1)
            consumed += 1
        if publish:
            queue.append((produced, produced & 1))
            produced += 1
        clear = cycle in (1000, 2000)
        dut.clear_i.value = clear
        dut.publish_i.value = publish
        dut.release_i.value = release
        if clear:
            queue.clear()
            produced = consumed = 0
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)


def test_pingpong_ctrl():
    run_cocotb("pingpong_ctrl", ["rtl/pingpong_ctrl.sv"], "tb.unit.test_pingpong_ctrl",
               build_name=Path(__file__).stem)
