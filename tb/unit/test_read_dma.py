"""Bit-exact DMA byte-copy tests using cocotbext-axi's read-only AXI RAM."""
from collections import deque
import logging
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer, with_timeout
from cocotbext.axi import AxiRamRead, AxiReadBus

from tb.common.sim import run_cocotb


def test_read_dma():
    run_cocotb("read_dma", ["rtl/read_dma.sv"],
               "tb.unit.test_read_dma", build_name="test_read_dma")


def split_command(addr, beats):
    """Independent byte-address reference; DMA performs no golden-kernel math."""
    while beats:
        count = min(beats, 16, (4096 - addr % 4096) // 8)
        yield addr, count
        addr = (addr + 8 * count) & 0xFFFFFFFF
        beats -= count


def pauses(seed):
    rng = random.Random(seed)
    while True:
        yield rng.random() < 0.35


class ReadBench:
    def __init__(self, dut):
        self.dut = dut
        dut.rst_ni.value = 0
        dut.cmd_valid_i.value = 0
        dut.cmd_addr_i.value = 0
        dut.cmd_beats_i.value = 0
        dut.data_ready_i.value = 0
        self.ram = AxiRamRead(AxiReadBus.from_prefix(dut, "m_axi"), dut.clk,
                              dut.rst_ni, reset_active_level=False, size=2**20)
        self.ram.log.setLevel(logging.ERROR)
        # Let AR requests accumulate independently of blocked R delivery.
        self.ram.ar_channel.queue_occupancy_limit = 64
        self.ram.write(0, random.Random(0xA11CE).randbytes(self.ram.size))
        self.expected_ar = deque()
        self.expected_data = deque()
        self.inflight = deque()
        self.outstanding = 0
        self.peak = 0
        self.ar_count = 0
        self.data_count = 0
        self.cmd_count = 0
        self.cycle = 0
        self.cmd_cycles = []
        self.ar_cycles = []
        self.data_run = 0
        self.max_data_run = 0
        self.stalled_ar = None
        self.stalled_data = None
        cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
        self.monitor_task = cocotb.start_soon(self.monitor())

    async def reset(self):
        self.dut.rst_ni.value = 0
        self.dut.cmd_valid_i.value = 0
        self.dut.data_ready_i.value = 0
        for _ in range(4):
            await RisingEdge(self.dut.clk)
        await FallingEdge(self.dut.clk)
        self.dut.rst_ni.value = 1
        await Timer(1, unit="ns")
        assert int(self.dut.error_o.value) == 0

    def expect(self, commands, fault_addr=None):
        for addr, beats in commands:
            self.expected_ar.extend(split_command(addr, beats))
            for index in range(beats):
                word_addr = (addr + index * 8) & 0xFFFFFFFF
                data = (bytes(8) if word_addr == fault_addr else
                        self.ram.read(word_addr % self.ram.size, 8))
                self.expected_data.append((int.from_bytes(data, "little"), index == beats - 1))

    async def send(self, commands, gaps=False):
        rng = random.Random(0xC0A1)
        for addr, beats in commands:
            if gaps:
                for _ in range(rng.randrange(4)):
                    await FallingEdge(self.dut.clk)
                    self.dut.cmd_valid_i.value = 0
            await FallingEdge(self.dut.clk)
            self.dut.cmd_addr_i.value = addr
            self.dut.cmd_beats_i.value = beats
            self.dut.cmd_valid_i.value = 1
            while True:
                await RisingEdge(self.dut.clk)
                if int(self.dut.cmd_ready_o.value):
                    break
        await FallingEdge(self.dut.clk)
        self.dut.cmd_valid_i.value = 0

    async def random_ready(self):
        rng = random.Random(0xBAD5EED)
        while True:
            await FallingEdge(self.dut.clk)
            self.dut.data_ready_i.value = int(rng.random() >= 0.35)

    async def drain(self):
        while self.expected_data or self.expected_ar or self.outstanding:
            await RisingEdge(self.dut.clk)
        await FallingEdge(self.dut.clk)

    async def monitor(self):
        dut = self.dut
        while True:
            await FallingEdge(dut.clk)
            await Timer(1, unit="ns")
            # Sample settled pre-edge values, matching what the RTL will accept.
            ar = tuple(int(getattr(dut, "m_axi_ar" + name).value) for name in
                       ("addr", "len", "id", "size", "burst", "cache", "prot",
                        "lock", "qos", "region"))
            arvalid = int(dut.m_axi_arvalid.value)
            arready = int(dut.m_axi_arready.value)
            valid = int(dut.data_valid_o.value)
            ready = int(dut.data_ready_i.value)
            data = (int(dut.data_o.value), int(dut.data_last_o.value))
            rlast = int(dut.m_axi_rlast.value)
            cmd_fire = int(dut.cmd_valid_i.value) and int(dut.cmd_ready_o.value)
            reset = not int(dut.rst_ni.value)
            assert int(dut.m_axi_rready.value) == ready
            assert valid == (int(dut.m_axi_rvalid.value) and not reset)
            assert data[0] == int(dut.m_axi_rdata.value)
            await RisingEdge(dut.clk)
            self.cycle += 1
            if reset:
                self.expected_ar.clear()
                self.expected_data.clear()
                self.inflight.clear()
                self.outstanding = 0
                self.stalled_ar = self.stalled_data = None
                continue
            self.cmd_count += bool(cmd_fire)
            if cmd_fire:
                self.cmd_cycles.append(self.cycle)
            self.data_run = self.data_run + 1 if valid and ready else 0
            self.max_data_run = max(self.max_data_run, self.data_run)
            if self.stalled_ar is not None:
                assert arvalid and ar == self.stalled_ar, "AR changed under backpressure"
            self.stalled_ar = ar if arvalid and not arready else None
            if self.stalled_data is not None:
                assert valid and data == self.stalled_data, "output changed under backpressure"
            self.stalled_data = data if valid and not ready else None
            if arvalid and arready:
                addr, length, *attrs = ar
                assert length <= 15
                assert addr % 8 == 0 and addr % 4096 + 8 * (length + 1) <= 4096
                assert attrs == [0, 3, 1, 3, 0, 0, 0, 0]
                assert self.expected_ar and (addr, length + 1) == self.expected_ar.popleft()
                self.inflight.append(length + 1)
                self.outstanding += 1
                self.ar_count += 1
                self.ar_cycles.append(self.cycle)
            if valid and ready:
                assert self.expected_data and data == self.expected_data.popleft()
                assert self.inflight, "R without AR"
                self.inflight[0] -= 1
                assert bool(rlast) == (self.inflight[0] == 0)
                if rlast:
                    self.inflight.popleft()
                    self.outstanding -= 1
                self.data_count += 1
            assert 0 <= self.outstanding <= 8
            self.peak = max(self.peak, self.outstanding)


@cocotb.test()
async def random_and_directed(dut):
    bench = ReadBench(dut)
    await bench.reset()
    rng = random.Random(0x5EED123)
    commands = [(0xFF8, 600), (0x1F88, 1), (0x2000, 16), (0x3FF0, 17), (0x4008, 513),
                (0xFFF80, 512), (0xFFFFFFF8, 17), (0, 0), (0x7FF8, 511)]
    commands += [(rng.randrange(2**17) * 8, rng.randint(1, 600)) for _ in range(25)]
    bench.expect(commands)
    await bench.send(commands[:5])
    for _ in range(80):
        await RisingEdge(dut.clk)
    assert bench.peak == 8, "did not fill all eight read credits"
    assert int(dut.cmd_ready_o.value) == 0, "command FIFO did not fill"
    assert bench.ar_count == 8 and bench.data_count == 0
    assert bench.ar_cycles[0] - bench.cmd_cycles[0] == 3
    bench.ram.ar_channel.set_pause_generator(pauses(0xA1))
    bench.ram.r_channel.set_pause_generator(pauses(0xA2))
    ready_task = cocotb.start_soon(bench.random_ready())
    await with_timeout(bench.send(commands[5:], gaps=True), 1, "ms")
    await with_timeout(bench.drain(), 1, "ms")
    assert bench.cmd_count == len(commands)
    assert bench.data_count == sum(beats for _, beats in commands)
    assert int(dut.error_o.value) == 0
    ready_task.cancel()

    # Inject errors through the library memory callback, retaining its AXI BFM.
    original_read = bench.ram._read
    fault_addr = 0x90000

    async def faulty_read(address, length):
        if address == fault_addr:
            raise IOError("directed SLVERR")
        return await original_read(address, length)

    bench.ram._read = faulty_read
    await FallingEdge(dut.clk)
    dut.data_ready_i.value = 1
    error_commands = [(fault_addr - 8, 3), (0x100, 1)]
    bench.expect(error_commands, fault_addr=fault_addr)
    await bench.send(error_commands)
    await with_timeout(bench.drain(), 20, "us")
    assert int(dut.error_o.value) == 1
    bench.expect([(0x200, 1)])
    await bench.send([(0x200, 1)])
    await with_timeout(bench.drain(), 20, "us")
    assert int(dut.error_o.value) == 1, "error was not sticky"

    # Abort queued commands, live ARs and a stalled response, then reuse the DMA.
    await FallingEdge(dut.clk)
    dut.data_ready_i.value = 0
    bench.expect([(0x1000, 600), (0x5000, 20)])
    await bench.send([(0x1000, 600), (0x5000, 20)])
    for _ in range(100):
        await RisingEdge(dut.clk)
    assert bench.outstanding > 0
    await FallingEdge(dut.clk)
    await bench.reset()
    bench.ram.ar_channel.clear_pause_generator()
    bench.ram.r_channel.clear_pause_generator()
    bench.ram.ar_channel.pause = False
    bench.ram.r_channel.pause = False
    dut.data_ready_i.value = 1
    bench.expect([(0x2FF8, 33)])
    await bench.send([(0x2FF8, 33)])
    await with_timeout(bench.drain(), 20, "us")
    assert int(dut.error_o.value) == 0
    assert bench.max_data_run >= 16, "read did not sustain one beat per cycle"
