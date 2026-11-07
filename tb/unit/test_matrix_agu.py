"""TLM bank layout, edge padding, and bit-exact two-array block integration."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import cocotb
import numpy as np
from cocotb.triggers import Timer

from tb.common.sim import run_cocotb
from compiler.analysis.tiling import HwConfig
from compiler.analysis.tlm import Ddr, Trace, _load_input_rows
from compiler.golden.layers import _exact_matmul, _windows, pad_input
from compiler.model.ir import load_model


def pack(values, width=8):
    return sum((int(value) & ((1 << width) - 1)) << (width * lane)
               for lane, value in enumerate(values))


@dataclass
class Block:
    name: str
    x: np.ndarray
    weights: np.ndarray
    kh: int = 3
    kw: int = 3
    stride: int = 1
    pad_t: int = 1
    pad_l: int = 1
    oh0: int = 0
    rows: int = 1
    q: int = 1
    pair: int = 0
    dw: bool = False
    dense: bool = False
    subsample: bool = False
    pad_value: int = -17
    kb0: int = 0
    kb1: int | None = None
    first: bool = True
    w_out: int | None = None

    def __post_init__(self):
        self.h_in, self.w_in, self.cs = self.x.shape
        if self.w_out is None:
            self.w_out = (self.w_in + 2 * self.pad_l - self.kw) // self.stride + 1
        self.kc_n = (self.kw * self.cs + 7) // 8
        self.tiles = self.kh * (self.kw if self.dw else self.kc_n)
        if self.kb1 is None:
            self.kb1 = self.tiles
        self.lo = max(0, self.oh0 * self.stride - self.pad_t)
        self.hi = min(self.h_in - 1,
                      (self.oh0 + self.rows - 1) * self.stride - self.pad_t + self.kh - 1)
        self.m = self.rows * self.w_out
        assert self.q * self.m <= 512
        assert self.q * (self.kb1 - self.kb0) <= 128

    def input_bank(self):
        """Exactly _load_input_rows: complete rows, or compact selected pixels."""
        if self.subsample:
            bank = self.x[self.oh0 * self.stride:(self.oh0 + self.rows) * self.stride:self.stride,
                          :self.w_out * self.stride:self.stride].tobytes()
        else:
            bank = self.x[self.lo:self.hi + 1].tobytes()
        # Compare the local loader helper with the executable TLM as well.
        ddr = Ddr({0x10000000: self.x.tobytes()}, size=max(1 << 20, self.x.nbytes))
        tlm_bank, _ = _load_input_rows(
            ddr, Trace(), SimpleNamespace(in_base=0x10000000), self,
            self.cs, self.oh0, self.rows, HwConfig())
        assert bank == tlm_bank
        return bank

    def models(self):
        """Produce ordered transfers and independent numpy/golden partial ACC."""
        nk = self.kb1 - self.kb0
        n_wrows = self.kh * self.kw if self.dw else self.q * nk * 8
        bank = np.zeros((2, n_wrows, 8), dtype=np.int8)
        acc = np.zeros((2, self.q * self.m, 8), dtype=np.int64)
        activations, beats = [], []
        mm = np.arange(self.m)
        oh, ow = self.oh0 + mm // self.w_out, mm % self.w_out
        # The stream reference uses logical tensor coordinates, never bank addresses.
        for tile in range(self.kb0, self.kb1):
            kr, span = divmod(tile, self.kw if self.dw else self.kc_n)
            ih = oh * self.stride + kr - self.pad_t
            rows = []
            for array in range(2):
                if self.dw:
                    kc = np.full(8, span)
                    channel = (2 * self.pair + array) * 8 + np.arange(8)
                else:
                    kc, channel = np.divmod(8 * span + np.arange(8), self.cs)
                iw = ow[:, None] * self.stride + kc[None, :] - self.pad_l
                valid = ((ih[:, None] >= 0) & (ih[:, None] < self.h_in) &
                         (iw >= 0) & (iw < self.w_in) & (kc[None, :] < self.kw))
                data = self.x[np.clip(ih, 0, self.h_in - 1)[:, None],
                              np.clip(iw, 0, self.w_in - 1),
                              np.clip(channel, 0, self.cs - 1)[None, :]]
                rows.append(np.where(valid, data, self.pad_value).astype(np.int8))
            for pp in range(self.q):
                wt = []
                for array in range(2):
                    if self.dw:
                        group = 2 * self.pair + array
                        tap = self.weights[kr, span, group * 8:(group + 1) * 8]
                        bank[array, tile] = tap
                        mat = np.diag(tap)
                    else:
                        mat = np.zeros((8, 8), dtype=np.int8)
                        for lane in range(8):
                            kc, channel = divmod(8 * span + lane, self.cs)
                            if kc < self.kw:
                                mat[lane] = self.weights[(2 * pp + array) * 8:
                                                        (2 * pp + array + 1) * 8,
                                                        kr, kc, channel]
                        base = (pp * nk + tile - self.kb0) * 8
                        bank[array, base:base + 8] = mat
                    wt.append(mat)
                    result = _exact_matmul(rows[array], mat)
                    np.testing.assert_array_equal(result, rows[array].astype(np.int64) @
                                                  mat.astype(np.int64))
                    acc[array, pp * self.m:(pp + 1) * self.m] += result
                for lane in range(8):
                    beats.append((pack(wt[0][lane]), pack(wt[1][lane])))
                for m in range(self.m):
                    tag = ((not (self.first and tile == self.kb0)) << 9) | (pp * self.m + m)
                    activations.append((pack(rows[0][m]), pack(rows[1][m]), tag, m == 0))
        return bank, acc, activations, beats

    def full_golden(self):
        """Independent full-window golden reference, including kernel-row padding."""
        geometry = SimpleNamespace(kh=self.kh, kw=self.kw, stride=self.stride,
                                   pad_t=self.pad_t, pad_l=self.pad_l,
                                   pad_b=self.kh + self.stride, pad_r=self.kw + self.stride,
                                   pad_value=self.pad_value,
                                   h_out=self.oh0 + self.rows, w_out=self.w_out)
        windows = _windows(pad_input(self.x, geometry), geometry)[self.oh0:self.oh0 + self.rows]
        if self.dw:
            sums = np.einsum('hwijc,ijc->hwc', windows.astype(np.int64),
                             self.weights.astype(np.int64)).reshape(self.m, self.cs)
            return np.stack([sums[:, (2 * self.pair + array) * 8:
                                      (2 * self.pair + array + 1) * 8] for array in range(2)])
        sums = _exact_matmul(windows.reshape(self.m, -1),
                             self.weights.reshape(self.q * 16, -1).T)
        return np.stack([np.concatenate([sums[:, (2 * pp + array) * 8:
                                                   (2 * pp + array + 1) * 8]
                                         for pp in range(self.q)], axis=0) for array in range(2)])


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0
        self.active = False
        self.rows = deque()
        self.beats = deque()
        self.last_row_cycle = None
        self.overlap = 0
        self.swap_cycles = []
        self.rng = np.random.default_rng(0xA6_7020)
        self.metrics = []

    async def step(self):
        dut = self.dut
        dut.clk.value = 0
        await Timer(5, unit='ns')
        if self.active:
            if int(dut.w_valid_o.value):
                assert self.beats, 'unexpected weight beat'
                got = (int(dut.w_row0_o.value), int(dut.w_row1_o.value))
                assert got == self.beats.popleft(), f'weight mismatch at {self.cycle}'
                assert int(dut.w_ready_o.value) == 3
                assert int(dut.weight_allow_i.value) == 3
                self.overlap += int(dut.a_valid_o.value)
            if int(dut.a_valid_o.value):
                assert self.rows, 'unexpected activation'
                got = (int(dut.a_row0_o.value), int(dut.a_row1_o.value),
                       int(dut.a_tag_o.value), bool(dut.a_swap_o.value))
                expected = self.rows.popleft()
                assert got == expected, (f'cycle {self.cycle}, remaining={len(self.rows)}, '
                                         f'{got} != {expected}')
                if not expected[3]:
                    assert self.cycle == self.last_row_cycle + 1, 'bubble inside a tile'
                else:
                    assert int(dut.shadow_full_o.value) == 3, 'swap without a full shadow'
                    self.swap_cycles.append(self.cycle)
                self.last_row_cycle = self.cycle
        dut.clk.value = 1
        await Timer(5, unit='ns')
        self.cycle += 1

    async def reset(self):
        for name in ('start_i', 'in_wr_en_i', 'w_wr_en_i', 'rd_valid_i',
                     'rdata_ready_i', 'rst_ni'):
            getattr(self.dut, name).value = 0
        self.dut.weight_allow_i.value = 3
        for _ in range(3):
            await self.step()
        self.dut.rst_ni.value = 1

    async def load(self, block, wbank):
        bank = block.input_bank()
        # Poison unused memory so that bottom padding cannot accidentally pass
        # by relying on unwritten BRAM being zero or pad_value.
        words = [int.from_bytes(bank[pos:pos + 8].ljust(8, b'\xa5'), 'little')
                 for pos in range(0, len(bank), 8)]
        words += [0xA5A5A5A5A5A5A5A5] * min(64, 8192 - len(words))
        assert len(words) <= 8192
        for address in range(max(len(words), wbank.shape[1])):
            self.dut.in_wr_en_i.value = int(address < len(words))
            self.dut.w_wr_en_i.value = int(address < wbank.shape[1])
            if address < len(words):
                self.dut.in_wr_addr_i.value = address
                self.dut.in_wr_data_i.value = words[address]
            if address < wbank.shape[1]:
                self.dut.w_wr_addr_i.value = address
                self.dut.w_wr_data0_i.value = pack(wbank[0, address])
                self.dut.w_wr_data1_i.value = pack(wbank[1, address])
            await self.step()
        self.dut.in_wr_en_i.value = 0
        self.dut.w_wr_en_i.value = 0

    async def run(self, block, *, stalls=False, load=True):
        wbank, expected, rows, beats = block.models()
        if load:
            await self.load(block, wbank)
        for name, value in dict(dw=block.dw, in_dense=block.dense, subsample=block.subsample,
                                stride=block.stride, kh=block.kh, kw=block.kw,
                                pad_t=block.pad_t, pad_l=block.pad_l,
                                w_in=block.w_in, w_out=block.w_out, cs=block.cs,
                                kc_n=block.kc_n, oh0=block.oh0, rows=block.rows,
                                lo=block.lo, hi=block.hi, m_blk=block.m, q=block.q, pair=block.pair,
                                kb0=block.kb0, kb1=block.kb1,
                                first_kblk=block.first, pad_value=block.pad_value & 255).items():
            getattr(self.dut, name + '_i').value = int(value)
        self.rows, self.beats = deque(rows), deque(beats)
        self.active = True
        self.last_row_cycle = None
        self.overlap = 0
        self.swap_cycles = []
        self.dut.start_i.value = 1
        start = self.cycle
        await self.step()
        self.dut.start_i.value = 0
        for _ in range(100000):
            self.dut.weight_allow_i.value = int(self.rng.integers(0, 4)) if stalls else 3
            await self.step()
            if int(self.dut.done_o.value):
                break
        else:
            raise AssertionError('AGU timeout')
        assert not self.rows and not self.beats, 'done before all transfers'
        assert self.last_row_cycle == self.cycle - 1, 'done must follow final array issue'
        assert not int(self.dut.busy_o.value)
        if block.m == 1 and not stalls:
            assert all(right - left == 16 for left, right in
                       zip(self.swap_cycles, self.swap_cycles[1:])), 'short-tile preload bubble'
        cycles = self.cycle - start
        self.metrics.append((block.name, len(rows), cycles, len(rows) / cycles, self.overlap))
        self.dut._log.info('%s: %d rows / %d cycles = %.4f rows/cycle, overlap=%d',
                           *self.metrics[-1])
        self.dut.weight_allow_i.value = 3
        self.active = False
        return expected

    async def drain(self, expected):
        # done means issued, not completed: allow array LATENCY=16 plus ACC=3.
        for _ in range(22):
            await self.step()
            assert not int(self.dut.done_o.value), 'done was not a one-cycle pulse'
        assert not int(self.dut.acc_busy_o.value)
        count = expected.shape[1]
        sent = received = 0
        self.dut.rdata_ready_i.value = 3
        for _ in range(count * 4 + 32):
            self.dut.clk.value = 0
            self.dut.rd_valid_i.value = 3 if sent < count else 0
            self.dut.rd_addr_i.value = min(sent, count - 1)
            await Timer(1, unit='ns')
            accepted = sent < count and int(self.dut.rd_ready_o.value) == 3
            valid = int(self.dut.rdata_valid_o.value)
            assert valid in (0, 3), 'array drains diverged'
            if valid:
                assert received < count
                for array in range(2):
                    got = int(getattr(self.dut, f'rdata{array}_o').value)
                    want = pack(expected[array, received], 32)
                    assert got == want, f'ACC array={array}, addr={received}: {got:#x} != {want:#x}'
                received += 1
            await self.step()
            sent += int(accepted)
            if received == count:
                break
        assert sent == received == count
        self.dut.rd_valid_i.value = 0
        self.dut.rdata_ready_i.value = 0
        await self.step()


def random_block(rng, name, *, h=7, w=8, cs=16, kh=3, stride=1, pad=1,
                 rows=2, oh0=0, q=1, dw=False, **kwargs):
    x = rng.integers(-128, 128, (h, w, cs), dtype=np.int16).astype(np.int8)
    shape = (kh, kh, cs) if dw else (16 * q, kh, kh, cs)
    weights = rng.integers(-128, 128, shape, dtype=np.int16).astype(np.int8)
    return Block(name, x, weights, kh=kh, kw=kh, stride=stride, pad_t=pad, pad_l=pad,
                 rows=rows, oh0=oh0, q=q, dw=dw, **kwargs)


@cocotb.test()
async def block_integration(dut):
    bench = Bench(dut)
    await bench.reset()
    rng = np.random.default_rng(0xC0_7020)
    # Real first-layer weights and real image widths preserve dense DMA alignment.
    for model_name in ('efficientnet-lite0', 'resnet50'):
        layer = load_model(model_name).layers[0]
        for row_count in (1, 2):
            block = Block(f'{model_name}-r{row_count}',
                          rng.integers(-128, 128, (layer.h_in, layer.w_in, 3),
                                       dtype=np.int16).astype(np.int8),
                          layer.weights[:16], kh=layer.kh, kw=layer.kw,
                          stride=layer.stride, pad_t=layer.pad_t, pad_l=layer.pad_l,
                          w_out=layer.w_out, rows=row_count, dense=True,
                          pad_value=layer.pad_value)
            expected = await bench.run(block)
            np.testing.assert_array_equal(expected, block.full_golden())
            await bench.drain(expected)

    cases = [
        random_block(rng, 'aligned3-s1-q4', q=4),
        random_block(rng, 'aligned3-s2', stride=2),
        random_block(rng, 'pointwise-q3', kh=1, pad=0, q=3, cs=24),
        random_block(rng, 'subsampled1-s2', kh=1, stride=2, pad=0, cs=24, subsample=True),
        random_block(rng, 'fc-many-tiles', h=1, w=1, cs=256, kh=1, pad=0, rows=1),
        random_block(rng, 'dw3-s1-p1', cs=32, dw=True, pair=1),
        random_block(rng, 'dw3-s2', stride=2, dw=True),
        random_block(rng, 'dw5-s2', kh=5, stride=2, pad=2, dw=True),
        random_block(rng, 'dense-full-bank', h=16, w=1024, cs=4, kh=8,
                     stride=8, pad=0, rows=2, dense=True),
        random_block(rng, 'aligned-interior-s3', h=13, stride=3, oh0=1),
    ]
    # Last output blocks: effective bottom padding is respectively 1, 1, 3, 2.
    # Unused bank words contain poison, so absent bottom masks corrupt both
    # the activation scoreboard and the independent golden-window ACC result.
    bottom_cases = [
        random_block(rng, 'aligned3-s1-bottom', oh0=5, q=2),
        random_block(rng, 'aligned3-s2-bottom', stride=2, oh0=2),
        random_block(rng, 'dense7-s2-bottom', h=9, w=16, cs=3, kh=7,
                     stride=2, pad=3, oh0=3, dense=True),
        random_block(rng, 'dw5-s2-bottom-p1', h=9, cs=32, kh=5,
                     stride=2, pad=2, oh0=3, dw=True, pair=1),
    ]
    for block in bottom_cases:
        assert block.hi == block.h_in - 1
        assert (block.oh0 + block.rows - 1) * block.stride - block.pad_t + block.kh > block.h_in
    cases.extend(bottom_cases)
    # Middle blocks load a strict subset of layer rows, with nonzero lo/hi.
    middle_cases = [
        random_block(rng, 'aligned3-s1-middle', h=13, oh0=4, q=2),
        random_block(rng, 'dense7-s2-middle', h=17, w=16, cs=3, kh=7,
                     stride=2, pad=3, oh0=3, dense=True),
    ]
    for block in middle_cases:
        assert 0 < block.lo < block.hi < block.h_in - 1
    cases.extend(middle_cases)
    cases.append(random_block(rng, 'subsampled1-s2-bottom', kh=1, stride=2, pad=0,
                              oh0=2, cs=24, subsample=True))
    # All dense channel counts, including gathers crossing left/row boundaries.
    for cs in range(1, 8):
        cases.append(random_block(rng, f'dense-cs{cs}', cs=cs, dense=True, q=2))
    for pad_value in (-128, 0, 127):
        for edge, oh0 in (('top', 0), ('bottom', 5)):
            directed = random_block(rng, f'directed-{edge}-pad{pad_value}', cs=8,
                                    oh0=oh0, pad_value=pad_value)
            directed.x.flat[::2] = -128
            directed.x.flat[1::2] = 127
            directed.weights[:] = 0 if pad_value == 0 else (-128 if pad_value < 0 else 127)
            cases.append(directed)
    for index, block in enumerate(cases):
        expected = await bench.run(block, stalls=index % 3 == 1 and block.name != 'fc-many-tiles')
        np.testing.assert_array_equal(expected, block.full_golden())
        await bench.drain(expected)

    # Two true k-blocks: q=4 forces 32 tiles/k-block; 3*3*32/8=36.
    block = random_block(rng, 'multi-kblk-first', cs=32, q=4, kb1=32)
    total = await bench.run(block)
    block.name, block.kb0, block.kb1, block.first = 'multi-kblk-second', 32, block.tiles, False
    total += await bench.run(block, stalls=True)
    np.testing.assert_array_equal(total, block.full_golden())
    await bench.drain(total)

    # Back-to-back starts with unchanged banks; no reset/drain between blocks.
    block = random_block(rng, 'reuse-arrays-1', h=1, w=1, kh=1, pad=0, rows=1, cs=64)
    await bench.run(block)
    block.name = 'reuse-arrays-2'
    expected = await bench.run(block, load=False)
    await bench.drain(expected)
    dut._log.info('TOTAL: %d rows / %d compute cycles = %.4f rows/cycle',
                  sum(item[1] for item in bench.metrics), sum(item[2] for item in bench.metrics),
                  sum(item[1] for item in bench.metrics) / sum(item[2] for item in bench.metrics))


def test_matrix_agu():
    run_cocotb('conv_block_tb',
              ['rtl/matrix_agu.sv', 'rtl/ram_2p.sv', 'rtl/ws_pe.sv', 'rtl/ws_array.sv',
               'rtl/accumulator.sv', 'tb/unit/conv_block_tb.sv'],
              'tb.unit.test_matrix_agu', build_name=Path(__file__).stem)
