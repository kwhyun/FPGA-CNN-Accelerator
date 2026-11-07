"""Tiling rule invariants for all layers of the three target models."""
from __future__ import annotations

import pytest

from compiler.analysis.tiling import MODELS, HwConfig, c8, plan_model
from compiler.model.ir import load_model


@pytest.mark.parametrize("name", MODELS)
def test_blocks_fit_buffers(name):
    cfg = HwConfig()
    layers = load_model(name).layers
    for lay, p in zip(layers, plan_model(name, cfg)):
        if lay.kind == "CONV":
            q = p.groups_per_blk
            assert 1 <= q <= 4 and p.m_blk * q <= cfg.acc_rows
            assert p.rows_per_blk >= 1 and p.n_mblk * p.rows_per_blk >= lay.h_out
        if lay.kind == "DWCONV":
            assert p.m_blk <= cfg.acc_rows
        assert p.cycles >= p.compute_cycles > 0
        assert p.out_bytes == lay.h_out * lay.w_out * c8(lay.c_out)
