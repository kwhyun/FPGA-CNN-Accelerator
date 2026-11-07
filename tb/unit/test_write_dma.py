"""AXI DMA writes checked against a byte/strobe model and cocotbext-axi RAM."""
from collections import deque
import logging
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer, with_timeout
from cocotbext.axi import AxiRamWrite, AxiWriteBus

from tb.common.sim import run_cocotb


def test_write_dma():
    run_cocotb("write_dma", ["rtl/write_dma.sv"],
               "tb.unit.test_write_dma", build_name="test_write_dma")


def split_command(addr, beats):
    """DMA is byte transport; no golden arithmetic kernel applies."""
    while beats:
        count = min(beats, 16, (4096 - addr % 4096) // 8)
        yield addr, count
        addr = (addr + count * 8) & 0xFFFFFFFF
        beats -= count


def pauses(seed):
    rng = random.Random(seed)
    while True:
        yield rng.random() < 0.35


class WriteBench:
    def __init__(self, dut):
        self.dut = dut
        dut.rst_ni.value = 0
        dut.cmd_valid_i.value = 0
        dut.cmd_addr_i.value = 0
        dut.cmd_beats_i.value = 0
        dut.data_valid_i.value = 0
        dut.data_i.value = 0
        dut.strb_i.value = 0
        self.ram = AxiRamWrite(AxiWriteBus.from_prefix(dut, "m_axi"), dut.clk,
                               dut.rst_ni, reset_active_level=False, size=2**20)
        self.ram.log.setLevel(logging.ERROR)
        self.ram.aw_channel.queue_occupancy_limit = 64
        self.ram.w_channel.queue_occupancy_limit = 64
        self.reference = bytearray(random.Random(0xDADA).randbytes(self.ram.size))
        self.ram.write(0, self.reference)
        self.expected_aw = deque()
        self.expected_w = deque()
        self.outstanding = 0
        self.peak = 0
        self.aw_count = 0
        self.w_count = 0
        self.b_count = 0
        self.cmd_count = 0
        self.cycle = 0
        self.cmd_cycles = []
        self.aw_cycles = []
        self.w_run = 0
        self.max_w_run = 0
        self.stalled_aw = None
        self.stalled_w = None
        cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
        self.monitor_task = cocotb.start_soon(self.monitor())

    async def reset(self):
        self.dut.rst_ni.value = 0
        self.dut.cmd_valid_i.value = 0
        self.dut.data_valid_i.value = 0
        for _ in range(4):
            await RisingEdge(self.dut.clk)
        await FallingEdge(self.dut.clk)
        self.dut.rst_ni.value = 1
        await Timer(1, unit="ns")
        assert int(self.dut.error_o.value) == 0
        assert int(self.dut.idle_o.value) == 1

    def expect(self, commands, seed):
        rng = random.Random(seed)
        payload = []
        for addr, beats in commands:
            bursts = list(split_command(addr, beats))
            self.expected_aw.extend(bursts)
            for burst_addr, count in bursts:
                for index in range(count):
                    data = rng.getrandbits(64)
                    # Exercise disabled writes, full writes and arbitrary byte lanes.
                    strb = rng.choice([0, 255, 0x55, 0xAA, rng.randrange(256)])
                    payload.append((data, strb))
                    self.expected_w.append((data, strb, index == count - 1))
                    byte_addr = ((burst_addr + 8 * index) & 0xFFFFFFFF) % self.ram.size
                    for lane in range(8):
                        if (strb >> lane) & 1:
                            self.reference[byte_addr + lane] = (data >> (8 * lane)) & 255
        return payload

    async def send_commands(self, commands, gaps=False):
        rng = random.Random(0xC04D)
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

    async def send_data(self, payload, gaps=False):
        rng = random.Random(0xBEA75)
        for data, strb in payload:
            if gaps:
                for _ in range(rng.randrange(4)):
                    await FallingEdge(self.dut.clk)
                    self.dut.data_valid_i.value = 0
            await FallingEdge(self.dut.clk)
            self.dut.data_i.value = data
            self.dut.strb_i.value = strb
            self.dut.data_valid_i.value = 1
            while True:
                await RisingEdge(self.dut.clk)
                if int(self.dut.data_ready_o.value):
                    break
        await FallingEdge(self.dut.clk)
        self.dut.data_valid_i.value = 0

    async def drain(self):
        while (self.expected_aw or self.expected_w or self.outstanding or
               not int(self.dut.idle_o.value)):
            await RisingEdge(self.dut.clk)
        await FallingEdge(self.dut.clk)

    async def monitor(self):
        dut = self.dut
        while True:
            await FallingEdge(dut.clk)
            await Timer(1, unit="ns")
            aw = tuple(int(getattr(dut, "m_axi_aw" + name).value) for name in
                       ("addr", "len", "id", "size", "burst", "cache", "prot",
                        "lock", "qos", "region"))
            awvalid = int(dut.m_axi_awvalid.value)
            awready = int(dut.m_axi_awready.value)
            w = (int(dut.m_axi_wdata.value), int(dut.m_axi_wstrb.value),
                 int(dut.m_axi_wlast.value))
            wvalid = int(dut.m_axi_wvalid.value)
            wready = int(dut.m_axi_wready.value)
            b_fire = int(dut.m_axi_bvalid.value) and int(dut.m_axi_bready.value)
            cmd_fire = int(dut.cmd_valid_i.value) and int(dut.cmd_ready_o.value)
            reset = not int(dut.rst_ni.value)
            await RisingEdge(dut.clk)
            self.cycle += 1
            if reset:
                self.expected_aw.clear()
                self.expected_w.clear()
                self.outstanding = 0
                self.stalled_aw = self.stalled_w = None
                continue
            self.cmd_count += bool(cmd_fire)
            if cmd_fire:
                self.cmd_cycles.append(self.cycle)
            self.w_run = self.w_run + 1 if wvalid and wready else 0
            self.max_w_run = max(self.max_w_run, self.w_run)
            if self.stalled_aw is not None:
                assert awvalid and aw == self.stalled_aw, "AW changed under backpressure"
            self.stalled_aw = aw if awvalid and not awready else None
            if self.stalled_w is not None:
                assert wvalid and w == self.stalled_w, "W changed under backpressure"
            self.stalled_w = w if wvalid and not wready else None
            if awvalid and awready:
                addr, length, *attrs = aw
                assert length <= 15
                assert addr % 8 == 0 and addr % 4096 + 8 * (length + 1) <= 4096
                assert attrs == [0, 3, 1, 3, 0, 0, 0, 0]
                assert self.expected_aw and (addr, length + 1) == self.expected_aw.popleft()
                self.outstanding += 1
                self.aw_count += 1
                self.aw_cycles.append(self.cycle)
            if wvalid and wready:
                assert self.expected_w and w == self.expected_w.popleft()
                self.w_count += 1
            if b_fire:
                self.outstanding -= 1
                self.b_count += 1
            assert 0 <= self.outstanding <= 8
            self.peak = max(self.peak, self.outstanding)


@cocotb.test()
async def random_and_directed(dut):
    bench = WriteBench(dut)
    await bench.reset()
    rng = random.Random(0x5EED456)
    commands = [(0xFF8, 600), (0x1F88, 1), (0x2000, 16), (0x3FF0, 17), (0x4008, 513),
                (0xFFF80, 512), (0xFFFFFFF8, 17), (0, 0), (0x7FF8, 511)]
    commands += [(rng.randrange(2**17) * 8, rng.randint(1, 600)) for _ in range(25)]
    payload = bench.expect(commands, 0x1234)
    # AW must make progress with no W source, up to exactly eight live B credits.
    bench.ram.b_channel.pause = True
    await bench.send_commands(commands[:5])
    for _ in range(80):
        await RisingEdge(dut.clk)
    assert bench.aw_count == 8 and bench.w_count == 0
    assert bench.peak == 8 and int(dut.idle_o.value) == 0
    assert bench.aw_cycles[0] - bench.cmd_cycles[0] == 3
    assert int(dut.cmd_ready_o.value) == 0
    data_task = cocotb.start_soon(bench.send_data(payload, gaps=True))
    # Delay B even after W begins, exercising stalls with all credits consumed.
    for _ in range(300):
        await RisingEdge(dut.clk)
    assert bench.aw_count == 8 and bench.b_count == 0
    bench.ram.b_channel.pause = False
    bench.ram.aw_channel.set_pause_generator(pauses(0xB1))
    bench.ram.w_channel.set_pause_generator(pauses(0xB2))
    bench.ram.b_channel.set_pause_generator(pauses(0xB3))
    await with_timeout(bench.send_commands(commands[5:], gaps=True), 1, "ms")
    await with_timeout(data_task, 1, "ms")
    await with_timeout(bench.drain(), 1, "ms")
    assert bench.cmd_count == len(commands)
    assert bench.w_count == sum(beats for _, beats in commands)
    assert bench.aw_count == bench.b_count
    assert bench.ram.read(0, bench.ram.size) == bench.reference
    assert int(dut.error_o.value) == 0

    # Conversely W must progress while its AW is blocked (including burst LAST).
    bench.ram.aw_channel.clear_pause_generator()
    bench.ram.w_channel.clear_pause_generator()
    bench.ram.b_channel.clear_pause_generator()
    bench.ram.w_channel.pause = False
    bench.ram.b_channel.pause = False
    bench.ram.aw_channel.pause = True
    old_aw, old_w = bench.aw_count, bench.w_count
    payload = bench.expect([(0x90000, 17)], 0xCAFE)
    await bench.send_commands([(0x90000, 17)])
    data_task = cocotb.start_soon(bench.send_data(payload))
    for _ in range(120):
        await RisingEdge(dut.clk)
    assert bench.aw_count == old_aw
    assert bench.w_count == old_w + 16, "W incorrectly depended on AWREADY"
    bench.ram.aw_channel.pause = False
    await with_timeout(data_task, 20, "us")
    await with_timeout(bench.drain(), 20, "us")
    assert bench.ram.read(0, bench.ram.size) == bench.reference
    assert bench.max_w_run >= 16, "write did not sustain one beat per cycle"

    # A failing library memory callback generates a real AXI SLVERR response.
    original_write = bench.ram._write

    async def faulty_write(address, data):
        if 0xA0000 <= address < 0xA0008:
            raise IOError("directed SLVERR")
        await original_write(address, data)

    bench.ram._write = faulty_write
    payload = bench.expect([(0xA0000, 1)], 0xEE)
    payload[0] = (payload[0][0], 255)
    bench.expected_w[-1] = (*payload[0], True)
    await bench.send_commands([(0xA0000, 1)])
    await bench.send_data(payload)
    await with_timeout(bench.drain(), 20, "us")
    assert int(dut.error_o.value) == 1
    payload = bench.expect([(0xA0100, 1)], 0xEF)
    await bench.send_commands([(0xA0100, 1)])
    await bench.send_data(payload)
    await with_timeout(bench.drain(), 20, "us")
    assert int(dut.error_o.value) == 1, "error was not sticky"

    # Reset with live AWs and queued commands, then verify a clean restart.
    bench.expected_aw.extend(split_command(0x1000, 600))
    bench.expected_aw.extend(split_command(0x5000, 20))
    await bench.send_commands([(0x1000, 600), (0x5000, 20)])
    for _ in range(100):
        await RisingEdge(dut.clk)
    assert bench.outstanding > 0
    await FallingEdge(dut.clk)
    await bench.reset()
    bench.reference[:] = bench.ram.read(0, bench.ram.size)
    payload = bench.expect([(0x2FF8, 33)], 0xFA)
    await bench.send_commands([(0x2FF8, 33)])
    await bench.send_data(payload)
    await with_timeout(bench.drain(), 20, "us")
    assert int(dut.error_o.value) == 0
    assert bench.ram.read(0, bench.ram.size) == bench.reference
