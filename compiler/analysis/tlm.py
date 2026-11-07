"""Transaction-level model of the accelerator dataflow.

Runs a layer from only the instruction fields and DDR contents. It follows the block plan (compiler.analysis.tiling),
DMA commands, SPAD/ACC addresses, im2col row construction and drain order the same way as the RTL.
Arithmetic uses round_shift from compiler.golden.layers. This is the reference model for the RTL sequencer and
address generators, and its results must be bit-exact with the golden model.

    python -m compiler.analysis.tlm --model resnet50
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np

from compiler.analysis.tiling import HwConfig, cdiv, plan_layer
from compiler.codegen.isa import KIND_NAMES, Instr, scalar1_as_int
from compiler.codegen.layout import c8
from compiler.golden.layers import round_shift


# ---------------------------------------------------------------------------
# DDR and trace
# ---------------------------------------------------------------------------
class Ddr:
    def __init__(self, regions: dict[int, bytes], size: int = 64 << 20, base: int = 0x1000_0000):
        self.base = base
        self.mem = bytearray(size)
        for addr, data in regions.items():
            self.write(addr, data)

    def read(self, addr: int, n: int) -> bytes:
        o = addr - self.base
        if addr % 8 or n % 8 or o < 0 or o + n > len(self.mem):
            raise ValueError(f"bad DDR read {addr:#x}+{n}")
        return bytes(self.mem[o: o + n])

    def write(self, addr: int, data: bytes) -> None:
        o = addr - self.base
        self.mem[o: o + len(data)] = data


@dataclass
class Trace:
    """DMA commands and compute statistics (for RTL comparison and cycle analysis)."""
    in_cmds: list[tuple[int, int]] = field(default_factory=list)    # (addr, bytes)
    w_cmds: list[tuple[int, int]] = field(default_factory=list)
    out_cmds: list[tuple[int, int]] = field(default_factory=list)
    rows_streamed: int = 0
    preloads: int = 0


def geom(ins: Instr) -> SimpleNamespace:
    return SimpleNamespace(
        index=-1, kind=KIND_NAMES[ins.layer_type], h_in=ins.h_in, w_in=ins.w_in, c_in=ins.c_in,
        h_out=ins.h_out, w_out=ins.w_out, c_out=ins.c_out, kh=ins.kh, kw=ins.kw, stride=ins.stride,
        pad_t=ins.pad_t, pad_l=ins.pad_l, pad_b=ins.pad_b, pad_r=ins.pad_r, in_dense=bool(ins.in_dense),
    )


def _requant(acc: np.ndarray, coef: np.ndarray, ins: Instr) -> np.ndarray:
    """acc int64 [8], coef int32 [8,2] → int8 [8]."""
    word = coef[:, 1].astype(np.int64) & 0xFFFFFFFF
    m, s = word & 0x1FFFF, (word >> 24) & 0x3F
    y = round_shift((acc + coef[:, 0].astype(np.int64)) * m, s) + ins.zp_out
    return np.clip(y, ins.act_min, ins.act_max).astype(np.int8)


def _m_blocks(g, rows_per_blk: int):
    for b in range(cdiv(g.h_out, rows_per_blk)):
        oh0 = b * rows_per_blk
        yield oh0, min(rows_per_blk, g.h_out - oh0)


def _in_row_range(g, oh0: int, r: int) -> tuple[int, int]:
    """Input rows [lo, hi] needed by a block (clipped to bounds)."""
    lo = max(0, oh0 * g.stride - g.pad_t)
    hi = min(g.h_in - 1, (oh0 + r - 1) * g.stride - g.pad_t + g.kh - 1)
    return lo, hi


# ---------------------------------------------------------------------------
# CONV / DWCONV
# ---------------------------------------------------------------------------
def _load_input_rows(ddr: Ddr, tr: Trace, ins: Instr, g, cs: int, oh0: int, r: int, cfg: HwConfig):
    """Builds the input bank contents and address function. Returns (bank bytes, addr(ih, iw) → bank byte offset or None)."""
    row_bytes = g.w_in * cs
    if g.kh == g.kw == 1 and g.stride > 1:   # subsampled 1×1: load only the block's output pixels, packed
        parts = []
        for oh in range(oh0, oh0 + r):
            for ow in range(g.w_out):
                a = ins.in_base + (oh * g.stride * g.w_in + ow * g.stride) * cs
                tr.in_cmds.append((a, cs))
                parts.append(ddr.read(a, cs))
        bank = b"".join(parts)

        def addr(ih, iw):
            ih, iw = np.asarray(ih), np.asarray(iw)
            ok = (ih % g.stride == 0) & (iw % g.stride == 0) & (ih >= 0) & (iw >= 0)
            oh, ow = ih // g.stride - oh0, iw // g.stride
            ok &= (oh >= 0) & (oh < r) & (ow < g.w_out)
            return np.where(ok, (oh * g.w_out + ow) * cs, -1)
        return bank, addr
    lo, hi = _in_row_range(g, oh0, r)
    nbytes = (hi - lo + 1) * row_bytes
    if nbytes > cfg.in_bank_bytes:
        raise AssertionError("input block exceeds bank")
    a = ins.in_base + lo * row_bytes
    # The DMA reads in 8 B aligned units (dense first layer: row bytes are a multiple of 8)
    tr.in_cmds.append((a, nbytes))
    bank = ddr.read(a, nbytes)

    def addr(ih, iw):
        ih, iw = np.asarray(ih), np.asarray(iw)
        ok = (ih >= lo) & (ih <= hi) & (iw >= 0) & (iw < g.w_in)
        return np.where(ok, (ih - lo) * row_bytes + iw * cs, -1)
    return bank, addr


def run_conv(ddr: Ddr, ins: Instr, cfg: HwConfig, tr: Trace) -> None:
    g = geom(ins)
    p = plan_layer(g, cfg)
    cs = g.c_in if g.in_dense else c8(g.c_in)
    kc_n = cdiv(g.kw * cs, 8)
    tiles_k = g.kh * kc_n
    n_tiles = c8(g.c_out) // 8
    q = p.groups_per_blk                    # n_pairs per block P
    n_nblk = cdiv(p.n_pairs, q)
    tiles_per_kblk = cfg.tiles_per_wbank // q
    c8o = c8(g.c_out)
    pv = np.int64(ins.pad_value)
    coef_all = np.frombuffer(ddr.read(ins.coef_base, c8o * 8), dtype="<i4").reshape(c8o, 2)

    for oh0, r in _m_blocks(g, p.rows_per_blk):
        bank, addr = _load_input_rows(ddr, tr, ins, g, cs, oh0, r, cfg)
        bank_arr = np.frombuffer(bank, dtype=np.int8).astype(np.int64)
        m_n = r * g.w_out
        for nb in range(n_nblk):
            acc = np.zeros((2, q * m_n, 8), dtype=np.int64)           # ACC[array][p·M + m]
            for kb0 in range(0, tiles_k, tiles_per_kblk):
                kb1 = min(tiles_k, kb0 + tiles_per_kblk)
                # weight bank: array a sub-bank row = (p·(kb1−kb0) + (t−kb0))·8 + k8
                wbank = np.zeros((2, q * (kb1 - kb0) * 8, 8), dtype=np.int64)
                for pp in range(q):
                    for a in range(2):
                        nt = 2 * (nb * q + pp) + a
                        if nt >= n_tiles:
                            continue
                        src = ins.in2_base + (nt * tiles_k + kb0) * 64
                        nbytes = (kb1 - kb0) * 64
                        tr.w_cmds.append((src, nbytes))
                        rows = np.frombuffer(ddr.read(src, nbytes), dtype=np.int8).reshape(-1, 8)
                        base = pp * (kb1 - kb0) * 8
                        wbank[a, base: base + len(rows)] = rows
                for t in range(kb0, kb1):
                    kr, j = divmod(t, kc_n)
                    # im2col: row for output pixel m = bytes [8j, 8j+8) of the kernel-row kr span
                    mm = np.arange(m_n)
                    oh, ow = oh0 + mm // g.w_out, mm % g.w_out
                    ih = (oh * g.stride + kr - g.pad_t)[:, None]
                    kc, c = np.divmod(8 * j + np.arange(8), cs)
                    off = addr(np.broadcast_to(ih, (m_n, 8)), (ow * g.stride)[:, None] + kc[None, :] - g.pad_l)
                    off = np.where(kc[None, :] < g.kw, off, -1)
                    arows = np.where(off >= 0, bank_arr[np.maximum(off, 0) + c[None, :]], pv)
                    for pp in range(q):
                        tr.preloads += 1
                        tr.rows_streamed += m_n
                        for a in range(2):
                            wt = wbank[a, (pp * (kb1 - kb0) + (t - kb0)) * 8:][:8]   # [k, n]
                            acc[a, pp * m_n: (pp + 1) * m_n] += arows @ wt
            # drain: per pixel, write P n_pairs × 2 arrays (16P B) contiguously
            ch0 = nb * q * 16
            nch = min(q * 16, c8o - ch0)
            tr.w_cmds.append((ins.coef_base + ch0 * 8, nch * 8))
            cols = []
            for pp in range(q):
                for a in range(2):
                    nt = 2 * (nb * q + pp) + a
                    if nt < n_tiles:
                        cols.append(_requant(acc[a, pp * m_n: (pp + 1) * m_n], coef_all[nt * 8: nt * 8 + 8], ins))
            out = np.concatenate(cols, axis=1)                       # [M, nch]
            for m in range(m_n):
                oh, ow = oh0 + m // g.w_out, m % g.w_out
                dst = ins.out_base + (oh * g.w_out + ow) * c8o + ch0
                tr.out_cmds.append((dst, out.shape[1]))
                ddr.write(dst, out[m].tobytes())


def run_dwconv(ddr: Ddr, ins: Instr, cfg: HwConfig, tr: Trace) -> None:
    g = geom(ins)
    p = plan_layer(g, cfg)
    cs = c8(g.c_in)
    groups = cs // 8
    if p.groups_per_blk != groups:
        raise AssertionError("DWCONV requires all channel groups in one input bank")
    pv = np.int64(ins.pad_value)
    taps = g.kh * g.kw
    wts = np.frombuffer(ddr.read(ins.in2_base, groups * taps * 8), dtype=np.int8).reshape(groups, taps, 8).astype(np.int64)
    coef_all = np.frombuffer(ddr.read(ins.coef_base, cs * 8), dtype="<i4").reshape(cs, 2)
    for oh0, r in _m_blocks(g, p.rows_per_blk):
        bank, addr = _load_input_rows(ddr, tr, ins, g, cs, oh0, r, cfg)
        bank_arr = np.frombuffer(bank, dtype=np.int8).astype(np.int64)
        m_n = r * g.w_out
        for pair in range(cdiv(groups, 2)):
            tr.w_cmds.append((ins.in2_base + 2 * pair * taps * 8, min(2, groups - 2 * pair) * taps * 8))
            acc = np.zeros((2, m_n, 8), dtype=np.int64)
            for t in range(taps):
                kr, kc = divmod(t, g.kw)
                tr.preloads += 1
                tr.rows_streamed += m_n
                mm = np.arange(m_n)
                oh, ow = oh0 + mm // g.w_out, mm % g.w_out
                off = addr(oh * g.stride + kr - g.pad_t, ow * g.stride + kc - g.pad_l)[:, None]
                for a in range(2):
                    grp = 2 * pair + a
                    if grp >= groups:
                        continue
                    idx = np.maximum(off, 0) + grp * 8 + np.arange(8)[None, :]
                    rows = np.where(off >= 0, bank_arr[idx], pv)
                    acc[a] += rows * wts[grp, t][None, :]       # block-diagonal: lane n = a[n]·w[n]
            tr.w_cmds.append((ins.coef_base + 2 * pair * 64, min(2, groups - 2 * pair) * 64))
            cols = [_requant(acc[a], coef_all[(2 * pair + a) * 8: (2 * pair + a) * 8 + 8], ins)
                    for a in range(2) if 2 * pair + a < groups]
            out = np.concatenate(cols, axis=1)
            for m in range(m_n):
                oh, ow = oh0 + m // g.w_out, m % g.w_out
                dst = ins.out_base + (oh * g.w_out + ow) * cs + 2 * pair * 8
                tr.out_cmds.append((dst, out.shape[1]))
                ddr.write(dst, out[m].tobytes())


# ---------------------------------------------------------------------------
# vector unit layers
# ---------------------------------------------------------------------------
def run_add(ddr: Ddr, ins: Instr, cfg: HwConfig, tr: Trace) -> None:
    nbytes = ins.h_in * ins.w_in * c8(ins.c_in)
    chunk = min(cfg.in_bank_bytes, cfg.w_bank_rows * 2 * 8)
    ma, s = ins.scalar0 & 0x1FFFF, (ins.scalar0 >> 24) & 0x3F
    mb = ins.scalar1 & 0x1FFFF
    for o in range(0, nbytes, chunk):
        n = min(chunk, nbytes - o)
        tr.in_cmds.append((ins.in_base + o, n))
        tr.w_cmds.append((ins.in2_base + o, n))
        a = np.frombuffer(ddr.read(ins.in_base + o, n), dtype=np.int8).astype(np.int64)
        b = np.frombuffer(ddr.read(ins.in2_base + o, n), dtype=np.int8).astype(np.int64)
        v = (a - ins.zp_in) * ma + (b - ins.zp_in2) * mb
        y = np.clip(round_shift(v, s) + ins.zp_out, ins.act_min, ins.act_max).astype(np.int8)
        tr.out_cmds.append((ins.out_base + o, n))
        ddr.write(ins.out_base + o, y.tobytes())


def run_maxpool(ddr: Ddr, ins: Instr, cfg: HwConfig, tr: Trace) -> None:
    g = geom(ins)
    p = plan_layer(g, cfg)
    cs = c8(g.c_in)
    for oh0, r in _m_blocks(g, p.rows_per_blk):
        bank, addr = _load_input_rows(ddr, tr, ins, g, cs, oh0, r, cfg)
        bank_arr = np.frombuffer(bank, dtype=np.int8).astype(np.int64)
        mm = np.arange(r * g.w_out)
        oh, ow = oh0 + mm // g.w_out, mm % g.w_out
        best = np.full((len(mm), cs), -129, dtype=np.int64)
        for kr in range(g.kh):                       # vector unit MAX: first..last rows (per group)
            for kc in range(g.kw):
                off = addr(oh * g.stride + kr - g.pad_t, ow * g.stride + kc - g.pad_l)[:, None]
                rows = np.where(off >= 0, bank_arr[np.maximum(off, 0) + np.arange(cs)[None, :]], ins.pad_value)
                best = np.maximum(best, rows)
        y = np.clip(best, ins.act_min, ins.act_max).astype(np.int8)
        for i in range(len(mm)):
            dst = ins.out_base + (oh[i] * g.w_out + ow[i]) * cs
            tr.out_cmds.append((dst, cs))
            ddr.write(dst, y[i].tobytes())


def run_avgpool(ddr: Ddr, ins: Instr, cfg: HwConfig, tr: Trace) -> None:
    cs = c8(ins.c_in)
    groups, npix = cs // 8, ins.h_in * ins.w_in
    gc = max(1, min(groups, cfg.in_bank_bytes // (npix * 8)))   # channel groups per bank
    m, s = ins.scalar0 & 0x1FFFF, (ins.scalar0 >> 24) & 0x3F
    bias = scalar1_as_int(ins.scalar1)
    for g0 in range(0, groups, gc):
        ng = min(gc, groups - g0)
        rows = []
        for pix in range(npix):
            a = ins.in_base + pix * cs + g0 * 8
            tr.in_cmds.append((a, ng * 8))
            rows.append(np.frombuffer(ddr.read(a, ng * 8), dtype=np.int8))
        total = np.stack(rows).astype(np.int64).sum(axis=0) + bias
        y = np.clip(round_shift(total * m, s) + ins.zp_out, ins.act_min, ins.act_max).astype(np.int8)
        tr.out_cmds.append((ins.out_base + g0 * 8, ng * 8))
        ddr.write(ins.out_base + g0 * 8, y.tobytes())


RUNNERS = {"CONV": run_conv, "DWCONV": run_dwconv, "ADD": run_add, "MAXPOOL": run_maxpool, "AVGPOOL": run_avgpool}


def run_instr(ddr: Ddr, ins: Instr, cfg: HwConfig | None = None) -> Trace:
    tr = Trace()
    RUNNERS[KIND_NAMES[ins.layer_type]](ddr, ins, cfg or HwConfig(), tr)
    return tr


def main() -> int:
    from compiler.codegen.layout import unpack_fmap
    from compiler.codegen.program import build_program
    from compiler.golden.run import run_model
    from compiler.model.ir import load_model

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--layers", type=int, default=0, help="number of leading layers to run (0 = all)")
    args = ap.parse_args()
    model = load_model(args.model)
    rng = np.random.default_rng(0)
    x = rng.integers(-128, 128, model.input_shape).astype(np.int8)
    prog = build_program(model, x)
    gold = run_model(model, x)
    ddr = Ddr(prog.regions)
    n = args.layers or len(model.layers)
    for lay, ins in zip(model.layers[:n], prog.instrs[:n]):
        run_instr(ddr, ins)
        got = unpack_fmap(ddr.read(ins.out_base, -(-lay.h_out * lay.w_out * c8(lay.c_out) // 8) * 8),
                          lay.h_out, lay.w_out, lay.c_out)
        ok = np.array_equal(got, gold[lay.output])
        print(f"L{lay.index:2d} {lay.kind:7s} {'OK' if ok else 'MISMATCH'}", flush=True)
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
