"""HW block/tile size rules and per-layer cycle / DDR traffic model.

The RTL sequencer must compute the same values as ``plan_layer``.
Cycles come from an ideal model: a 64-bit AXI port moves 8 B per cycle, each burst costs ``burst_overhead`` cycles,
and double buffering fully overlaps compute, reads and writes. These are not board measurements.

    python -m compiler.analysis.tiling [--model resnet50] [--sweep]
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass

from compiler.model.config import BUILD_DIR
from compiler.model.ir import Layer, load_model

MODELS = ("efficientnet-lite0", "mobilenet_v2", "resnet50")


@dataclass(frozen=True)
class HwConfig:
    in_bank_bytes: int = 64 * 1024      # one input_spad bank (ping or pong)
    w_bank_rows: int = 1024             # rows (8 B) per array sub-bank of one weight_spad bank
    acc_rows: int = 512                 # rows (8×INT32) per array in one accumulator bank
    preload_cycles: int = 16            # minimum tile swap spacing (ws_array: per-row shadow release)
    burst_overhead: int = 4             # extra cycles per AXI burst (model parameter)
    max_burst_bytes: int = 128          # 16 beat
    in_ports: int = 2                   # read ports of the input_spad compute bank
    pair_rule: str = "4"                # n_pairs per CONV block P: "1", "4", "best" (min over 1/2/4/8)
    dw_rule: str = "all_groups"         # "all_groups": load all channel groups together, "max_rows": max R by ACC
    subsample_1x1: bool = True          # 1×1 stride>1: load only the needed pixels

    @property
    def tiles_per_wbank(self) -> int:
        return self.w_bank_rows // 8


def c8(c: int) -> int:
    return -(-c // 8) * 8


def cdiv(a: int, b: int) -> int:
    return -(-a // b)


@dataclass
class Plan:
    layer: int
    kind: str
    order: str = "m_outer"   # m_outer: input resident, n_outer: weights resident
    rows_per_blk: int = 0    # output rows R
    m_blk: int = 0           # R × W_out
    n_mblk: int = 0
    n_pairs: int = 0
    tiles_k: int = 0
    k_blks: int = 0
    groups_per_blk: int = 0  # DWCONV/POOL: 8-channel groups loaded together into the input bank
    macs: int = 0
    compute_cycles: int = 0
    in_bytes: int = 0
    in_bursts: int = 0
    w_bytes: int = 0
    w_bursts: int = 0
    out_bytes: int = 0
    out_bursts: int = 0
    cycles: int = 0
    bound: str = ""


def _bursts(nbytes: int, chunk: int, cfg: HwConfig) -> int:
    """Number of bursts to read nbytes from a contiguous region in chunk-byte bursts."""
    if nbytes == 0:
        return 0
    return cdiv(nbytes, chunk) * cdiv(chunk, cfg.max_burst_bytes)


def _subsampled(lay: Layer, cfg: HwConfig) -> bool:
    return cfg.subsample_1x1 and lay.kh == lay.kw == 1 and lay.stride > 1


def _in_rows(r: int, lay: Layer, cfg: HwConfig | None = None) -> int:
    if cfg is not None and _subsampled(lay, cfg):
        return r
    return min((r - 1) * lay.stride + lay.kh, lay.h_in + lay.pad_t + lay.pad_b)


def in_rows_loaded(lay: Layer, oh0: int, r: int, cfg: HwConfig) -> int:
    """Input rows the DMA actually reads for output rows [oh0, oh0+r) (clipped to bounds)."""
    if _subsampled(lay, cfg):
        return r
    lo = max(0, oh0 * lay.stride - lay.pad_t)
    hi = min(lay.h_in - 1, (oh0 + r - 1) * lay.stride - lay.pad_t + lay.kh - 1)
    return hi - lo + 1


def block_rows(lay: Layer, rows_per_blk: int) -> list[tuple[int, int]]:
    return [(oh0, min(rows_per_blk, lay.h_out - oh0)) for oh0 in range(0, lay.h_out, rows_per_blk)]


def _in_cols(lay: Layer, cfg: HwConfig) -> int:
    return lay.w_out if _subsampled(lay, cfg) else lay.w_in


def _rows_per_blk(lay: Layer, pixel_bytes: int, cfg: HwConfig, acc_div: int = 1) -> int:
    """Largest output row count R that fits the ACC (acc_rows/acc_div) and the input bank."""
    if lay.w_out > cfg.acc_rows // acc_div:
        return 0
    r_acc = (cfg.acc_rows // acc_div) // lay.w_out
    r = min(r_acc, lay.h_out)
    while r > 0 and _in_rows(r, lay, cfg) * _in_cols(lay, cfg) * pixel_bytes > cfg.in_bank_bytes:
        r -= 1
    return r


def _finish(p: Plan, cfg: HwConfig) -> Plan:
    in_c = p.in_bytes // 8 + p.in_bursts * cfg.burst_overhead
    w_c = p.w_bytes // 8 + p.w_bursts * cfg.burst_overhead
    out_c = p.out_bytes // 8 + p.out_bursts * cfg.burst_overhead
    parts = {"compute": p.compute_cycles, "in": in_c, "weight": w_c, "out": out_c}
    p.bound = max(parts, key=parts.get)
    p.cycles = max(parts.values())
    return p


def plan_conv(lay: Layer, cfg: HwConfig) -> Plan:
    n_pairs = cdiv(c8(lay.c_out) // 8, 2)
    if cfg.pair_rule == "best":
        cands = [q for q in (1, 2, 4, 8) if q <= max(1, n_pairs)]
    else:
        # fixed rule: P = min(rule value, n_pair, ACC rows / W_out)
        cands = [max(1, min(int(cfg.pair_rule), n_pairs, cfg.acc_rows // lay.w_out))]
    best = None
    for q in cands:
        p = _plan_conv_p(lay, cfg, q)
        if p is not None and (best is None or p.cycles < best.cycles):
            best = p
    if best is None:
        raise ValueError(f"layer {lay.index}: no feasible CONV block")
    return best


def _plan_conv_p(lay: Layer, cfg: HwConfig, q: int) -> Plan | None:
    cs = lay.c_in if lay.in_dense else c8(lay.c_in)
    p = Plan(lay.index, lay.kind)
    p.tiles_k = lay.kh * cdiv(lay.kw * cs, 8)
    p.n_pairs = cdiv(c8(lay.c_out) // 8, 2)
    p.groups_per_blk = q  # CONV: n_pairs per block P
    p.k_blks = cdiv(p.tiles_k * q, cfg.tiles_per_wbank)
    p.rows_per_blk = _rows_per_blk(lay, cs, cfg, acc_div=q)
    if p.rows_per_blk == 0:
        return None
    p.m_blk = p.rows_per_blk * lay.w_out
    p.n_mblk = cdiv(lay.h_out, p.rows_per_blk)
    p.macs = lay.h_out * lay.w_out * lay.c_out * lay.kh * lay.kw * lay.c_in
    cols = _in_cols(lay, cfg)
    in_bytes_once = sum(in_rows_loaded(lay, oh0, r, cfg) * cols * cs for oh0, r in block_rows(lay, p.rows_per_blk))
    w_total = c8(lay.c_out) * p.tiles_k * 8
    n_nblk = cdiv(p.n_pairs, q)
    t_m = in_bytes_once + w_total * p.n_mblk
    t_n = in_bytes_once * n_nblk + w_total
    if p.k_blks == 1 and p.n_mblk > 1 and t_n < t_m:
        p.order = "n_outer"
        p.in_bytes, p.w_bytes = in_bytes_once * n_nblk, w_total
    else:
        p.in_bytes, p.w_bytes = in_bytes_once, w_total * p.n_mblk
    chunk = cs if _subsampled(lay, cfg) else lay.w_in * cs
    p.in_bursts = _bursts(p.in_bytes, max(chunk, 8), cfg)
    w_chunk = cdiv(lay.kw * cs, 8) * 64
    p.w_bursts = _bursts(p.w_bytes, w_chunk, cfg)
    rows_last = lay.h_out - (p.n_mblk - 1) * p.rows_per_blk
    m_sizes = [p.m_blk] * (p.n_mblk - 1) + [rows_last * lay.w_out]
    p.compute_cycles = sum(p.n_pairs * p.tiles_k * max(m, cfg.preload_cycles) for m in m_sizes)
    # output: per pixel, the block's P n_pairs are contiguous (16P B)
    p.out_bytes = lay.h_out * lay.w_out * c8(lay.c_out)
    p.out_bursts = lay.h_out * lay.w_out * n_nblk * cdiv(min(16 * q, c8(lay.c_out)), cfg.max_burst_bytes)
    return _finish(p, cfg)


def plan_dw(lay: Layer, cfg: HwConfig) -> Plan:
    p = Plan(lay.index, lay.kind)
    groups = c8(lay.c_in) // 8
    p.n_pairs = cdiv(groups, 2)
    p.tiles_k = lay.kh * lay.kw
    if cfg.dw_rule == "all_groups":
        p.rows_per_blk = _rows_per_blk(lay, c8(lay.c_in), cfg)   # all channel groups in one bank
    else:
        p.rows_per_blk = _rows_per_blk(lay, 16, cfg)
    if p.rows_per_blk == 0:
        raise ValueError(f"layer {lay.index}: no feasible DWCONV block")
    p.m_blk = p.rows_per_blk * lay.w_out
    p.n_mblk = cdiv(lay.h_out, p.rows_per_blk)
    rows_in = _in_rows(p.rows_per_blk, lay)
    p.groups_per_blk = min(groups, max(2, (cfg.in_bank_bytes // (rows_in * lay.w_in * 8)) // 2 * 2))
    p.macs = lay.h_out * lay.w_out * lay.c_in * lay.kh * lay.kw
    pix = sum(in_rows_loaded(lay, oh0, r, cfg) * lay.w_in for oh0, r in block_rows(lay, p.rows_per_blk))
    p.in_bytes = pix * c8(lay.c_in)
    p.in_bursts = _bursts(p.in_bytes, lay.w_in * c8(lay.c_in), cfg)
    p.w_bytes = p.n_mblk * groups * p.tiles_k * 8
    p.w_bursts = p.n_mblk * cdiv(groups, 2)
    rows_last = lay.h_out - (p.n_mblk - 1) * p.rows_per_blk
    m_sizes = [p.m_blk] * (p.n_mblk - 1) + [rows_last * lay.w_out]
    p.compute_cycles = sum(p.n_pairs * p.tiles_k * max(m, cfg.preload_cycles) for m in m_sizes)
    p.out_bytes = lay.h_out * lay.w_out * c8(lay.c_out)
    p.out_bursts = lay.h_out * lay.w_out * p.n_pairs
    return _finish(p, cfg)


def plan_add(lay: Layer, cfg: HwConfig) -> Plan:
    p = Plan(lay.index, lay.kind)
    nbytes = lay.h_in * lay.w_in * c8(lay.c_in)
    chunk = min(cfg.in_bank_bytes, cfg.w_bank_rows * 2 * 8)
    p.n_mblk = cdiv(nbytes, chunk)
    p.in_bytes = p.w_bytes = p.out_bytes = nbytes
    p.in_bursts = p.w_bursts = p.out_bursts = _bursts(nbytes, nbytes, cfg)
    p.compute_cycles = nbytes // 8
    return _finish(p, cfg)


def plan_pool(lay: Layer, cfg: HwConfig) -> Plan:
    p = Plan(lay.index, lay.kind)
    groups = c8(lay.c_in) // 8
    nbytes = lay.h_in * lay.w_in * c8(lay.c_in)
    if lay.kind == "MAXPOOL":
        p.rows_per_blk = _rows_per_blk(lay, c8(lay.c_in), cfg) or 1
        p.n_mblk = cdiv(lay.h_out, p.rows_per_blk)
        p.compute_cycles = cdiv(lay.h_out * lay.w_out * groups * lay.kh * lay.kw, cfg.in_ports)
    else:
        p.n_mblk = cdiv(nbytes, cfg.in_bank_bytes)
        p.compute_cycles = cdiv(lay.h_in * lay.w_in * groups, cfg.in_ports)
    p.in_bytes = nbytes
    p.in_bursts = _bursts(nbytes, lay.w_in * c8(lay.c_in), cfg)
    p.out_bytes = lay.h_out * lay.w_out * c8(lay.c_out)
    p.out_bursts = _bursts(p.out_bytes, p.out_bytes, cfg)
    return _finish(p, cfg)


def plan_layer(lay: Layer, cfg: HwConfig) -> Plan:
    return {"CONV": plan_conv, "DWCONV": plan_dw, "ADD": plan_add,
            "MAXPOOL": plan_pool, "AVGPOOL": plan_pool}[lay.kind](lay, cfg)


def plan_model(name: str, cfg: HwConfig) -> list[Plan]:
    return [plan_layer(l, cfg) for l in load_model(name).layers]


def summarize(plans: list[Plan]) -> dict:
    tot = sum(p.cycles for p in plans)
    macs = sum(p.macs for p in plans)
    by_kind: dict[str, int] = {}
    by_bound: dict[str, int] = {}
    for p in plans:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + p.cycles
        by_bound[p.bound] = by_bound.get(p.bound, 0) + p.cycles
    return {
        "cycles": tot, "ms_at_100mhz": tot / 1e5, "macs": macs,
        "mac_utilization": macs / (tot * 128) if tot else 0.0,
        "cycles_by_kind": by_kind, "cycles_by_bound": by_bound,
        "ddr_read_bytes": sum(p.in_bytes + p.w_bytes for p in plans),
        "ddr_write_bytes": sum(p.out_bytes for p in plans),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=MODELS)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--layers", action="store_true")
    ap.add_argument("--rules", action="store_true")
    args = ap.parse_args()
    models = [args.model] if args.model else list(MODELS)
    out_dir = BUILD_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    ap_rules = getattr(args, "rules", False)
    if ap_rules:
        for pr in ("1", "4", "best"):
            for dw in ("max_rows", "all_groups"):
                for sub in (False, True):
                    cfg = HwConfig(pair_rule=pr, dw_rule=dw, subsample_1x1=sub)
                    tot = {m: summarize(plan_model(m, cfg))["cycles"] for m in models}
                    print(f"pair={pr:4s} dw={dw:10s} sub1x1={sub!s:5s} " + " ".join(f"{m}={v/1e5:.2f}ms" for m, v in tot.items()))
        return 0
    if args.sweep:
        rows = []
        for ib in (32, 64, 96):
            for acc in (256, 512, 1024):
                for wr in (1024, 2048):
                    for ov in (0, 4, 8):
                        cfg = HwConfig(in_bank_bytes=ib * 1024, acc_rows=acc, w_bank_rows=wr, burst_overhead=ov)
                        row = {"in_bank_kib": ib, "acc_rows": acc, "w_bank_rows": wr, "burst_overhead": ov}
                        for m in models:
                            try:
                                row[m] = summarize(plan_model(m, cfg))["cycles"]
                            except ValueError:
                                row[m] = None
                        rows.append(row)
        (out_dir / "sweep.json").write_text(json.dumps(rows, indent=1))
        for r in rows:
            print(r)
        return 0
    cfg = HwConfig()
    for m in models:
        plans = plan_model(m, cfg)
        s = summarize(plans)
        (out_dir / f"{m}.json").write_text(json.dumps({"config": asdict(cfg), "summary": s,
                                                       "layers": [asdict(p) for p in plans]}, indent=1))
        print(f"{m}: {s['cycles']:,} cycles = {s['ms_at_100mhz']:.2f} ms, MAC util {s['mac_utilization']:.1%}, "
              f"by kind { {k: f'{v/1e5:.2f}ms' for k, v in s['cycles_by_kind'].items()} }, "
              f"bound { {k: f'{v/1e5:.2f}ms' for k, v in s['cycles_by_bound'].items()} }")
        if args.layers:
            for p in plans:
                print(f"  L{p.layer:2d} {p.kind:7s} {p.order:7s} R={p.rows_per_blk:3d} M={p.m_blk:3d} nm={p.n_mblk:3d} "
                      f"np={p.n_pairs:3d} tk={p.tiles_k:4d} kb={p.k_blks} cyc={p.cycles:8,d} {p.bound}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
