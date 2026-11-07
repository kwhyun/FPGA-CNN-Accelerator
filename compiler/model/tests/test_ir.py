"""IR invariants on the three target models."""
from __future__ import annotations

import collections

import numpy as np
import pytest

from compiler.model.ir import M_MAX, M_MIN, load_model, word_ms

EXPECTED = {
    "efficientnet-lite0": {"CONV": 34, "DWCONV": 16, "ADD": 9, "AVGPOOL": 1},
    "mobilenet_v2": {"CONV": 36, "DWCONV": 17, "ADD": 10, "AVGPOOL": 1},
    "resnet50": {"CONV": 54, "MAXPOOL": 1, "ADD": 16, "AVGPOOL": 1},
}


@pytest.mark.parametrize("name", EXPECTED)
def test_layer_counts(name):
    m = load_model(name)
    assert dict(collections.Counter(l.kind for l in m.layers)) == EXPECTED[name]


@pytest.mark.parametrize("name", EXPECTED)
def test_chaining_and_ranges(name):
    m = load_model(name)
    shapes = {m.input_id: m.input_shape}
    for lay in m.layers:
        for t in lay.inputs:
            assert shapes[t] == (lay.h_in, lay.w_in, lay.c_in), (lay.index, t)
        shapes[lay.output] = (lay.h_out, lay.w_out, lay.c_out)
        assert lay.h_out == (lay.h_in + lay.pad_t + lay.pad_b - lay.kh) // lay.stride + 1
        assert lay.w_out == (lay.w_in + lay.pad_l + lay.pad_r - lay.kw) // lay.stride + 1
        assert max(lay.kh, lay.kw) <= 7 and lay.stride in (1, 2) and max(lay.pad_t, lay.pad_l, lay.pad_b, lay.pad_r) <= 7
        assert max(lay.h_in, lay.w_in, lay.c_in, lay.c_out) < 4096
        if lay.kind != "CONV":
            assert lay.c_in == lay.c_out
        if lay.mult is not None:
            ms = np.array([word_ms(int(w)) for w in lay.mult])
            assert ((ms[:, 0] >= M_MIN) & (ms[:, 0] < M_MAX)).all()
            assert ((ms[:, 1] >= 1) & (ms[:, 1] <= 63)).all()
        assert -128 <= lay.act_min <= lay.zp_out <= lay.act_max <= 127 or lay.act_min == -128
    assert m.layers[0].in_dense and not any(l.in_dense for l in m.layers[1:])
    assert m.num_classes - m.class_offset == 1000
