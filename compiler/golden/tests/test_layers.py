"""Golden kernels vs independent naive loops (docs/arithmetic.md)."""
from __future__ import annotations

import itertools

import numpy as np
import pytest

from compiler.golden import layers as G
from compiler.model.ir import Layer, mant_shift, mult_word, word_ms


def _rs(x: int, s: int) -> int:
    mag = (abs(x) + (1 << (s - 1))) >> s
    return -mag if x < 0 else mag


@pytest.mark.parametrize("x,s,expected", [
    (1 << 15, 16, 1), (-(1 << 15), 16, -1),            # ±0.5 → ±1 (away from zero)
    (3 << 15, 16, 2), (-(3 << 15), 16, -2),            # ±1.5 → ±2
    ((1 << 15) - 1, 16, 0), (-(1 << 15) + 1, 16, 0),
    (5, 1, 3), (-5, 1, -3), (7, 3, 1), (0, 40, 0),
])
def test_round_shift(x, s, expected):
    assert int(G.round_shift(np.int64(x), s)) == expected


@pytest.mark.parametrize("r", [1.7e-9, 6.5e-5, 0.0021, 0.4999999, 0.5, 1.0, 1.9999999])
def test_mant_shift(r):
    m, s = mant_shift(r)
    assert (1 << 16) <= m < (1 << 17)
    assert abs(m / 2.0**s - r) / r <= 2.0**-17
    assert word_ms(mult_word(m, s)) == (m, s)


def _layer(kind, h, w, c_in, c_out, k=1, stride=1, pads=(0, 0, 0, 0), rng=None, **kw):
    pt, pl, pb, pr = pads
    h_out = (h + pt + pb - k) // stride + 1
    w_out = (w + pl + pr - k) // stride + 1
    lay = Layer(index=0, name="t", kind=kind, inputs=[0], output=1, h_in=h, w_in=w, c_in=c_in,
                h_out=h_out, w_out=w_out, c_out=c_out, kh=k, kw=k, stride=stride,
                pad_t=pt, pad_l=pl, pad_b=pb, pad_r=pr, **kw)
    if kind in ("CONV", "DWCONV"):
        lay.weights = (rng.integers(-127, 128, (c_out, k, k, c_in)) if kind == "CONV"
                       else rng.integers(-127, 128, (k, k, c_in))).astype(np.int8)
        lay.eff_bias = rng.integers(-5000, 5000, c_out).astype(np.int32)
        lay.mult = np.array([mult_word(*mant_shift(r)) for r in rng.uniform(1e-4, 0.02, c_out)], np.int32)
    return lay


def _requant_naive(acc, lay, n):
    m, s = word_ms(int(lay.mult[n]))
    v = _rs((acc + int(lay.eff_bias[n])) * m, s) + lay.zp_out
    return min(max(v, lay.act_min), lay.act_max)


def _at(x, lay, ih, iw, c):
    if 0 <= ih < lay.h_in and 0 <= iw < lay.w_in:
        return int(x[ih, iw, c])
    return lay.pad_value


CASES = [  # (k, stride, pads)
    (1, 1, (0, 0, 0, 0)), (1, 2, (0, 0, 0, 0)), (3, 1, (1, 1, 1, 1)), (3, 2, (0, 0, 1, 1)),
    (5, 2, (1, 2, 2, 1)), (7, 2, (3, 3, 3, 3)),
]


@pytest.mark.parametrize("k,stride,pads", CASES)
def test_conv(k, stride, pads):
    rng = np.random.default_rng(k * 10 + stride)
    lay = _layer("CONV", 9, 8, 3, 5, k, stride, pads, rng, zp_in=-3, pad_value=-3, zp_out=4, act_min=-20, act_max=100)
    x = rng.integers(-128, 128, (lay.h_in, lay.w_in, lay.c_in)).astype(np.int8)
    y = G.conv(x, lay)
    for oh, ow, n in itertools.product(range(lay.h_out), range(lay.w_out), range(lay.c_out)):
        acc = 0
        for i, j, c in itertools.product(range(k), range(k), range(lay.c_in)):
            acc += _at(x, lay, oh * stride + i - pads[0], ow * stride + j - pads[1], c) * int(lay.weights[n, i, j, c])
        assert y[oh, ow, n] == _requant_naive(acc, lay, n)


@pytest.mark.parametrize("k,stride,pads", CASES[2:5])
def test_dwconv(k, stride, pads):
    rng = np.random.default_rng(100 + k)
    lay = _layer("DWCONV", 9, 10, 6, 6, k, stride, pads, rng, zp_in=7, pad_value=7, zp_out=-2)
    x = rng.integers(-128, 128, (lay.h_in, lay.w_in, lay.c_in)).astype(np.int8)
    y = G.dwconv(x, lay)
    for oh, ow, c in itertools.product(range(lay.h_out), range(lay.w_out), range(lay.c_in)):
        acc = sum(_at(x, lay, oh * stride + i - pads[0], ow * stride + j - pads[1], c) * int(lay.weights[i, j, c])
                  for i in range(k) for j in range(k))
        assert y[oh, ow, c] == _requant_naive(acc, lay, c)


def test_maxpool():
    rng = np.random.default_rng(1)
    lay = _layer("MAXPOOL", 7, 7, 4, 4, 3, 2, (1, 1, 1, 1), rng, pad_value=-128, act_min=-128, act_max=127)
    x = rng.integers(-128, 128, (7, 7, 4)).astype(np.int8)
    y = G.maxpool(x, lay)
    for oh, ow, c in itertools.product(range(lay.h_out), range(lay.w_out), range(4)):
        v = max(_at(x, lay, oh * 2 + i - 1, ow * 2 + j - 1, c) for i in range(3) for j in range(3))
        assert y[oh, ow, c] == v


def test_add():
    rng = np.random.default_rng(2)
    lay = _layer("ADD", 3, 4, 5, 5, rng=rng, zp_in=-5, zp_in2=9, zp_out=-128, act_min=-128, act_max=127)
    _, s = mant_shift(0.9)
    lay.scalar0 = mult_word(int(round(0.9 * 2**s)), s)
    lay.scalar1 = int(round(0.3 * 2**s))
    a = rng.integers(-128, 128, (3, 4, 5)).astype(np.int8)
    b = rng.integers(-128, 128, (3, 4, 5)).astype(np.int8)
    y = G.add(a, b, lay)
    ma, s = word_ms(lay.scalar0)
    for idx in np.ndindex(a.shape):
        v = _rs((int(a[idx]) + 5) * ma + (int(b[idx]) - 9) * lay.scalar1, s) - 128
        assert y[idx] == min(max(v, -128), 127)


def test_avgpool():
    rng = np.random.default_rng(3)
    lay = _layer("AVGPOOL", 7, 7, 6, 6, rng=rng, zp_in=-4, zp_out=-128)
    lay.h_out = lay.w_out = 1
    lay.scalar0 = mult_word(*mant_shift(0.7 / 49))
    lay.scalar1 = -49 * -4
    x = rng.integers(-128, 128, (7, 7, 6)).astype(np.int8)
    y = G.avgpool(x, lay)
    m, s = word_ms(lay.scalar0)
    for c in range(6):
        v = _rs((int(x[:, :, c].astype(np.int64).sum()) + lay.scalar1) * m, s) - 128
        assert y[0, 0, c] == min(max(v, -128), 127)
