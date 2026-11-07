"""Bit-exact default tiling plans, production instructions, and start/reset protocol."""
from collections import Counter
from pathlib import Path
import random
from types import SimpleNamespace

import cocotb
from cocotb.triggers import Timer

from tb.common.sim import run_cocotb
from compiler.analysis.tiling import HwConfig, c8, cdiv, plan_layer
from compiler.codegen.isa import KIND_NAMES, KINDS
from compiler.codegen.program import layer_instr
from compiler.model.ir import load_model


INPUTS = ("layer_type", "kh", "kw", "stride", "pad_t", "pad_l", "pad_b", "pad_r",
          "in_dense", "h_in", "w_in", "h_out", "w_out", "c_in", "c_out")
OUTPUTS = ("cs", "kc_n", "tiles_k", "n_tiles", "n_pairs", "pairs_per_blk", "n_nblk",
           "tiles_per_kblk", "k_blks", "rows_per_blk", "m_blk", "n_mblk", "n_outer",
           "subsample", "avg_groups")
CFG = HwConfig()


def geometry(kind="CONV", **updates):
    fields = dict(index=-1, kind=kind, kh=1, kw=1, stride=1, pad_t=0, pad_l=0,
                  pad_b=0, pad_r=0, in_dense=False, h_in=8, w_in=8, h_out=8,
                  w_out=8, c_in=8, c_out=8)
    fields.update(updates)
    return SimpleNamespace(**fields)


def instruction_geometry(ins):
    fields = {name: int(getattr(ins, name)) for name in INPUTS if name != "layer_type"}
    return geometry(KIND_NAMES[ins.layer_type], **fields)


def expected_plan(lay):
    """Map actual Plan members verbatim; derive only extra interface quantities."""
    plan = plan_layer(lay, CFG)
    cs = lay.c_in if lay.in_dense else c8(lay.c_in)
    pairs = plan.groups_per_blk if lay.kind == "CONV" else 1
    values = dict(cs=cs, kc_n=cdiv(lay.kw * cs, 8), tiles_k=plan.tiles_k,
                  n_tiles=c8(lay.c_out) // 8, n_pairs=plan.n_pairs,
                  pairs_per_blk=pairs, n_nblk=cdiv(plan.n_pairs, pairs),
                  tiles_per_kblk=CFG.tiles_per_wbank // pairs, k_blks=plan.k_blks,
                  rows_per_blk=plan.rows_per_blk, m_blk=plan.m_blk,
                  n_mblk=plan.n_mblk, n_outer=int(plan.order == "n_outer"),
                  subsample=int(lay.kind == "CONV" and lay.kh == lay.kw == 1 and
                                lay.stride > 1),
                  avg_groups=(min(c8(lay.c_in) // 8,
                                  CFG.in_bank_bytes // (lay.h_in * lay.w_in * 8))
                              if lay.kind == "AVGPOOL" else 0))
    assert set(values) == set(OUTPUTS)
    return values


def directed_geometries():
    return [
        geometry(h_in=1, w_in=1, h_out=1, w_out=1, c_in=1, c_out=1, in_dense=True),
        geometry(kh=3, kw=3, pad_t=1, pad_b=1, pad_l=1, pad_r=1,
                 h_in=17, w_in=19, h_out=17, w_out=19, c_in=3, c_out=48, in_dense=True),
        geometry(h_in=1025, w_in=1, h_out=1025, w_out=1, c_out=1),
        geometry(h_in=1, w_in=1, h_out=1, w_out=1, c_in=4095, c_out=4095),
        geometry(h_in=128, w_in=64, h_out=128, w_out=64, c_out=33),  # q=3
        geometry(h_in=128, w_in=3, h_out=128, w_out=3,
                 c_in=1, c_out=128),  # equal order costs must select m_outer
        geometry(h_in=64, w_in=8, h_out=64, w_out=8, c_in=256, c_out=128),
        geometry(h_in=64, w_in=8, h_out=64, w_out=8, c_in=264, c_out=128),
        geometry(h_in=64, w_in=8, h_out=64, w_out=8, c_in=336, c_out=48),
        geometry(h_in=64, w_in=8, h_out=64, w_out=8, c_in=344, c_out=48),
        geometry(h_in=229, w_in=229, h_out=115, w_out=115, stride=2,
                 c_in=24, c_out=128),  # subsampled 1x1 capacity and traffic
        geometry(h_in=224, w_in=224, h_out=112, w_out=112, stride=2,
                 c_in=3, c_out=32, in_dense=True),
        geometry("DWCONV", h_in=229, w_in=229, h_out=115, w_out=115, stride=2,
                 c_in=24, c_out=24),  # helper subsampling also applies to DW
        geometry("MAXPOOL", h_in=229, w_in=229, h_out=115, w_out=115, stride=2,
                 c_in=24, c_out=24),
        geometry("MAXPOOL", h_in=3, w_in=513, h_out=3, w_out=513),  # R=1 fallback
        geometry("MAXPOOL", kh=7, kw=7, h_in=230, w_in=230, h_out=224, w_out=224,
                 c_in=2100, c_out=2100),  # bank-fit fallback
        geometry("AVGPOOL", h_in=230, w_in=230, h_out=1, w_out=1,
                 c_in=2100, c_out=2100),  # avg_groups=0 per interface formula
        geometry("AVGPOOL", h_in=1, w_in=1, h_out=1, w_out=1,
                 c_in=4095, c_out=4095),
        geometry("AVGPOOL", h_in=64, w_in=128, h_out=1, w_out=1,
                 c_in=17, c_out=17),  # exactly one group fits
        geometry("AVGPOOL", h_in=65, w_in=127, h_out=1, w_out=1,
                 c_in=17, c_out=17),
        geometry("ADD", h_in=4095, w_in=4095, h_out=4095, w_out=4095,
                 c_in=4095, c_out=4095),  # 36-bit nbytes / 22-bit n_mblk
        geometry("ADD", h_in=64, w_in=32, h_out=64, w_out=32),
        geometry("ADD", h_in=65, w_in=32, h_out=65, w_out=32),
        geometry(h_in=4095, w_in=256, h_out=4095, w_out=256,
                 c_in=256, c_out=4095),  # order comparison exceeds 32 bits
        geometry(h_in=1, w_in=256, h_out=7, w_out=256, pad_t=3, pad_b=3,
                 c_in=256, c_out=128),  # negative Python loaded-row sum
        geometry(kh=7, kw=5, stride=2, pad_t=3, pad_b=3, pad_l=2, pad_r=2,
                 h_in=61, w_in=59, h_out=31, w_out=30, c_in=8, c_out=64),
        geometry("DWCONV", kh=7, kw=5, stride=2, pad_t=3, pad_b=3, pad_l=2, pad_r=2,
                 h_in=61, w_in=59, h_out=31, w_out=30, c_in=129, c_out=129),
    ]


def random_geometries():
    rng = random.Random(0x7020_100)
    valid, errors = [], []
    kinds = tuple(KINDS)
    attempts = 0
    while len(valid) < 300:
        kind = kinds[len(valid) % len(kinds)]
        hi, wi = rng.randint(1, 230), rng.randint(1, 230)
        ci, co = rng.randint(1, 2100), rng.randint(1, 2100)
        kh, kw = rng.choice((1, 3, 5, 7)), rng.choice((1, 3, 5, 7))
        stride = rng.choice((1, 2))
        pt, pl, pb, pr = (rng.randint(0, 3) for _ in range(4))
        dense = kind == "CONV" and rng.random() < 0.2
        if dense:
            ci = rng.randint(1, 7)
        if kind != "CONV":
            co = ci
        if kind == "ADD":
            kh = kw = stride = 1
            pt = pl = pb = pr = 0
            ho, wo = hi, wi
        elif kind == "AVGPOOL":
            ho = wo = 1
        else:
            ho = (hi + pt + pb - kh) // stride + 1
            wo = (wi + pl + pr - kw) // stride + 1
        if ho <= 0 or wo <= 0:
            continue
        lay = geometry(kind, kh=kh, kw=kw, stride=stride, pad_t=pt, pad_l=pl,
                       pad_b=pb, pad_r=pr, in_dense=dense, h_in=hi, w_in=wi,
                       h_out=ho, w_out=wo, c_in=ci, c_out=co)
        attempts += 1
        assert attempts < 5000
        try:
            expected_plan(lay)
        except ValueError:
            if len(errors) < 30:
                errors.append(lay)
        else:
            valid.append(lay)
    assert Counter(lay.kind for lay in valid) == dict.fromkeys(kinds, 60)
    return valid, errors


async def tick(dut):
    dut.clk.value = 0
    await Timer(5, unit="ns")
    dut.clk.value = 1
    await Timer(5, unit="ns")


def drive(dut, lay):
    for name in INPUTS:
        value = KINDS[lay.kind] if name == "layer_type" else int(getattr(lay, name))
        getattr(dut, name + "_i").value = value


def snapshot(dut):
    return {name: int(getattr(dut, name + "_o").value) for name in OUTPUTS}


async def check_plan(dut, lay, *, disturb=False, hold_cycles=2):
    try:
        expected = expected_plan(lay)
    except ValueError:
        expected = None
    drive(dut, lay)
    dut.start_i.value = 1
    await tick(dut)
    assert not int(dut.done_o.value)
    assert int(dut.error_o.value) == 0
    dut.start_i.value = 0
    for cycles in range(1, 6501):
        # Captured fields must survive arbitrary live inputs and busy starts.
        if disturb and cycles == 2:
            drive(dut, geometry("ADD", c_in=4095))
            dut.start_i.value = 1
        if disturb and cycles == 3:
            dut.start_i.value = 0
        await tick(dut)
        if int(dut.done_o.value):
            break
    else:
        raise AssertionError(f"planner timeout: {vars(lay)}")
    assert int(dut.error_o.value) == int(expected is None), vars(lay)
    actual = snapshot(dut)
    if expected is not None:
        assert actual == expected, (vars(lay), actual, expected)
    for _ in range(hold_cycles):
        await tick(dut)
        assert not int(dut.done_o.value), "done must pulse for exactly one cycle"
        assert snapshot(dut) == actual, "plan changed before the next start"
        assert int(dut.error_o.value) == int(expected is None), "error must hold"
    return cycles, expected


@cocotb.test()
async def plans_bit_exact(dut):
    dut.start_i.value = 0
    dut.rst_ni.value = 0
    drive(dut, geometry())
    for _ in range(3):
        await tick(dut)
        assert not int(dut.done_o.value)
        assert not int(dut.error_o.value)
    dut.rst_ni.value = 1

    latencies, plans = [], []
    production_count = 0
    for name in ("efficientnet-lite0", "mobilenet_v2", "resnet50"):
        model = load_model(name)
        for layer in model.layers:
            # Use the compiler's instruction fields, including folded padding
            # and the first layer's in_dense setting.
            lay = instruction_geometry(layer_instr(layer))
            cycles, values = await check_plan(dut, lay, disturb=production_count % 13 == 0)
            assert values is not None, (name, layer.index)
            latencies.append(cycles)
            plans.append(values)
            production_count += 1
    assert production_count == 196
    model_latency = (min(latencies), max(latencies))

    random_valid, random_errors = random_geometries()
    for index, lay in enumerate(directed_geometries() + random_valid):
        cycles, values = await check_plan(dut, lay, disturb=index % 11 == 0)
        assert values is not None, vars(lay)
        latencies.append(cycles)
        plans.append(values)
    assert {p["pairs_per_blk"] for p in plans} == {1, 2, 3, 4}
    assert {p["n_outer"] for p in plans} == {0, 1}
    assert any(p["rows_per_blk"] == 512 for p in plans)
    assert any(p["subsample"] for p in plans)
    assert any(p["k_blks"] > 1 for p in plans)
    assert {p["avg_groups"] for p in plans} >= {0, 1, 512}

    # Python raises for ACC overflow and input-bank overflow, including DW.
    errors = [geometry(w_in=513, w_out=513),
              geometry("DWCONV", w_in=513, w_out=513),
              geometry(kh=7, kw=7, h_in=230, w_in=230, h_out=224, w_out=224,
                       c_in=2100, c_out=128),
              geometry("DWCONV", kh=7, kw=7, h_in=230, w_in=230,
                       h_out=224, w_out=224, c_in=2100, c_out=2100)] + random_errors
    for lay in errors:
        try:
            expected_plan(lay)
        except ValueError:
            pass
        else:
            raise AssertionError(f"error vector is feasible: {vars(lay)}")
        _, values = await check_plan(dut, lay, disturb=True)
        assert values is None

    # Reset must abort both a serial multiply and a serial divide. No stale
    # completion may appear during a subsequent idle interval or new request.
    for interrupt_after in (3, 30, 150):
        drive(dut, geometry(h_in=230, h_out=230, c_in=257, c_out=2100))
        dut.start_i.value = 1
        await tick(dut)
        dut.start_i.value = 0
        for _ in range(interrupt_after):
            await tick(dut)
            assert not int(dut.done_o.value)
        dut.rst_ni.value = 0
        await tick(dut)
        assert not int(dut.done_o.value)
        assert not int(dut.error_o.value)
        dut.rst_ni.value = 1
        for _ in range(5):
            await tick(dut)
            assert not int(dut.done_o.value)
        await check_plan(dut, geometry("ADD"))

    # Unsupported opcodes and zero dimensions are diagnosed without dividing
    # by zero. These are outside plan_layer's valid input-domain contract.
    for field, value in (("layer_type_i", 15), ("w_out_i", 0), ("h_in_i", 0),
                         ("c_in_i", 0), ("stride_i", 0), ("kh_i", 0)):
        drive(dut, geometry())
        getattr(dut, field).value = value
        dut.start_i.value = 1
        await tick(dut)
        dut.start_i.value = 0
        await tick(dut)
        assert int(dut.done_o.value) and int(dut.error_o.value)
        await tick(dut)
        assert not int(dut.done_o.value)
    await check_plan(dut, geometry())
    # With no idle bubbles, a new start is accepted on the edge immediately
    # after done. A layer with latency L therefore has initiation interval L+1.
    for lay in (geometry("ADD"), geometry("AVGPOOL"), geometry(), geometry("DWCONV")):
        await check_plan(dut, lay, hold_cycles=0)
    dut._log.info("Matched %d model layers, 300 random valid and %d directed plans; "
                  "%d infeasible plans. Observed latency %d..%d cycles after start.",
                  production_count, len(directed_geometries()), len(errors),
                  min(latencies), max(latencies))
    dut._log.info("Model-layer latency %d..%d cycles after start; earliest next "
                  "start is one cycle after done.", *model_latency)


def test_plan_unit():
    run_cocotb("plan_unit", ["rtl/plan_unit.sv"], "tb.unit.test_plan_unit",
               build_name=Path(__file__).stem)
