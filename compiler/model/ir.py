"""INT8 QDQ ONNX graph → accelerator layer IR (docs/ISA.md, docs/arithmetic.md).

The input QuantizeLinear, Flatten/Reshape (1×1×C) and the final Softmax are removed; Pad is folded into the next Conv/MaxPool.
Relu/Clip fold into the previous layer's clamp. Gemm (FC) becomes a 1×1 CONV; global average (GlobalAveragePool/ReduceMean) becomes AVGPOOL.
Supported: Conv (group 1 / depthwise), Gemm, Add, MaxPool, global average. Activations int8/uint8 per-tensor, weights symmetric int8.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

M_MIN, M_MAX = 1 << 16, 1 << 17
S_MIN, S_MAX = 1, 63
INT32_MIN, INT32_MAX = -(1 << 31), (1 << 31) - 1



@dataclass
class Layer:
    index: int
    name: str
    kind: str  # CONV | DWCONV | ADD | MAXPOOL | AVGPOOL
    inputs: list[int]
    output: int
    h_in: int
    w_in: int
    c_in: int
    h_out: int
    w_out: int
    c_out: int
    kh: int = 1
    kw: int = 1
    stride: int = 1
    pad_t: int = 0
    pad_l: int = 0
    pad_b: int = 0
    pad_r: int = 0
    pad_value: int = 0
    zp_in: int = 0
    zp_in2: int = 0
    zp_out: int = 0
    act_min: int = -128
    act_max: int = 127
    scale_in: float = 0.0
    scale_in2: float = 0.0
    scale_out: float = 0.0
    weights: np.ndarray | None = None  # CONV [Cout,kh,kw,Cin], DWCONV [kh,kw,C]
    eff_bias: np.ndarray | None = None  # int32 [Cout]
    mult: np.ndarray | None = None  # int32 [Cout] scale word {M, S}
    scalar0: int = 0
    scalar1: int = 0
    in_dense: bool = False
    op: str = ""  # original ONNX op


@dataclass
class Model:
    name: str
    layers: list[Layer]
    input_id: int
    input_shape: tuple[int, int, int]
    input_scale: float
    input_zp: int
    output_id: int
    num_classes: int
    class_offset: int
    onnx_path: Path | None = None
    tensor_names: dict[int, str] = field(default_factory=dict)  # tensor id → ONNX quantized tensor name
    uint8_tensors: frozenset[int] = frozenset()  # tensors that are uint8 in ONNX (IR uses int8 = uint8 − 128)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def mant_shift(real: float) -> tuple[int, int]:
    """Scale r → (M, S), r ≈ M / 2^S, 2^16 ≤ M < 2^17 (docs/arithmetic.md)."""
    if not real > 0:
        raise ValueError(f"multiplier must be positive, got {real!r}")
    m_frac, exp = np.frexp(real)  # real = m_frac · 2^exp, 0.5 ≤ m_frac < 1 → floor(log2 r) = exp − 1
    s = 16 - (int(exp) - 1)
    m = _round_half_away(real * 2.0**s)
    if m == M_MAX:
        m, s = M_MIN, s - 1
    if not (S_MIN <= s <= S_MAX) or not (M_MIN <= m < M_MAX):
        raise ValueError(f"multiplier {real!r} -> M={m}, S={s} out of range")
    return m, s


def mult_word(m: int, s: int) -> int:
    """32-bit scale word: [16:0] = M, [29:24] = S."""
    return (s << 24) | m


def word_ms(word: int) -> tuple[int, int]:
    return word & 0x1FFFF, (word >> 24) & 0x3F


def scaled(real: float, s: int) -> int:
    """Mantissa round(r·2^S) at a shared shift S (smaller side of ADD)."""
    m = _round_half_away(real * 2.0**s)
    if not (0 <= m < M_MAX):
        raise ValueError(f"shared-shift mantissa {m} out of range")
    return m


def _round_half_away(x: float) -> int:
    return int(np.sign(x) * np.floor(abs(x) + 0.5))


def same_pad(in_size: int, out_size: int, k: int, s: int) -> tuple[int, int]:
    total = max((out_size - 1) * s + k - in_size, 0)
    return total // 2, total - total // 2


def _conv_coeffs(
    w: np.ndarray, bias: np.ndarray, s_in: float, zp_in: int, s_w: np.ndarray, s_out: float
) -> tuple[np.ndarray, np.ndarray]:
    """eff_bias = bias − zp_in·Σw, scale word = {M, S} of s_in·s_w/s_out. w: [Cout, K].

    A channel with scale r < 2^−32 (near-zero weights) always yields zp_out when |acc + bias| < 2^31.
    Such channels get eff_bias = 0, {M_MIN, 63}: |acc|·M < 2^48 < 2^62 (rounding boundary), so they also yield zp_out.
    """
    if s_w.size == 1:
        s_w = np.full(w.shape[0], s_w[0])
    real = s_in * s_w.astype(np.float64) / s_out
    dead = real < 2.0**-32
    wsum = w.reshape(w.shape[0], -1).astype(np.int64).sum(axis=1)
    eff = np.where(dead, 0, bias.astype(np.int64) - zp_in * wsum)
    if eff.min() < INT32_MIN or eff.max() > INT32_MAX:
        raise ValueError("eff_bias outside int32")
    mult = np.array([mult_word(M_MIN, S_MAX) if d else mult_word(*mant_shift(float(r))) for r, d in zip(real, dead)],
                    dtype=np.int64)
    return eff.astype(np.int32), mult.astype(np.int32)


# ---------------------------------------------------------------------------
# ONNX graph access
# ---------------------------------------------------------------------------
_PASS = ("Flatten", "Reshape", "Squeeze", "Unsqueeze", "Identity")


class _Onnx:
    def __init__(self, path: Path):
        import onnx
        from onnx import numpy_helper, shape_inference

        self.g = shape_inference.infer_shapes(onnx.load(str(path))).graph
        self.const = {t.name: numpy_helper.to_array(t) for t in self.g.initializer}
        for n in self.g.node:
            if n.op_type == "Constant":
                a = next(a for a in n.attribute if a.name == "value")
                self.const[n.output[0]] = numpy_helper.to_array(a.t)
        self.shape: dict[str, list[int]] = {}
        for vi in [*self.g.input, *self.g.value_info, *self.g.output]:
            self.shape[vi.name] = [int(d.dim_value) for d in vi.type.tensor_type.shape.dim]
        self.prod = {o: n for n in self.g.node for o in n.output}
        self.cons: dict[str, list] = defaultdict(list)
        for n in self.g.node:
            for i in n.input:
                self.cons[i].append(n)
        self.graph_in = next(i.name for i in self.g.input if i.name not in self.const)

    @staticmethod
    def attr(node, name: str, default=None):
        from onnx import helper

        for a in node.attribute:
            if a.name == name:
                v = helper.get_attribute_value(a)
                return v.decode() if isinstance(v, bytes) else v
        return default

    def qparams(self, node) -> tuple[float, int, bool]:
        """Per-tensor (scale, int8 zp, is_uint8) of a Q/DQ node."""
        s = np.asarray(self.const[node.input[1]], np.float64).ravel()
        z = self.const[node.input[2]] if len(node.input) > 2 and node.input[2] else np.zeros(1, np.uint8)
        if s.size != 1:
            raise ValueError(f"{node.name}: activation must be per-tensor quantized")
        z = np.asarray(z)
        if z.dtype == np.uint8:
            return float(s[0]), int(z.ravel()[0]) - 128, True
        if z.dtype == np.int8:
            return float(s[0]), int(z.ravel()[0]), False
        raise ValueError(f"{node.name}: activation dtype {z.dtype} is unsupported (int8/uint8 only)")

    def hwc(self, name: str) -> tuple[int, int, int]:
        sh = self.shape.get(name)
        if not sh or sh[0] != 1:
            raise ValueError(f"{name}: unknown or non-batch-1 shape {sh}")
        if len(sh) == 4:
            return sh[2], sh[3], sh[1]
        if len(sh) == 2:
            return 1, 1, sh[1]
        raise ValueError(f"{name}: unsupported rank {len(sh)}")

    def const_dq(self, name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        """Constant DequantizeLinear input → (integer values, scale[], zp[], axis)."""
        n = self.prod.get(name)
        if n is None or n.op_type != "DequantizeLinear" or n.input[0] not in self.const:
            raise ValueError(f"{name}: weight/bias is not a quantized constant")
        q = self.const[n.input[0]]
        s = np.asarray(self.const[n.input[1]], np.float64).ravel()
        z = (np.asarray(self.const[n.input[2]]).ravel() if len(n.input) > 2 and n.input[2]
             else np.zeros(1, np.int64))
        return q, s, z.astype(np.int64), int(self.attr(n, "axis", 1))


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------
def load_model(name: str) -> Model:
    return _load_model_cached(name)


@lru_cache(maxsize=8)
def _load_model_cached(name: str) -> Model:
    from compiler.model.quantize import quantized_path

    return build_model(name, quantized_path(name))


def _weights(o: _Onnx, name: str, c_out: int, axis: int) -> tuple[np.ndarray, np.ndarray]:
    w, s_w, z_w, ax = o.const_dq(name)
    if w.dtype != np.int8:
        raise ValueError(f"{name}: weight dtype {w.dtype} (int8 only)")
    if np.any(z_w != 0):
        raise ValueError(f"{name}: weight zero point must be 0")
    if s_w.size not in (1, c_out) or (s_w.size > 1 and ax % w.ndim != axis):
        raise ValueError(f"{name}: weight must be per-tensor or per-output-channel")
    return w, s_w


def _bias(o: _Onnx, name: str | None, c_out: int, s_in: float, s_w: np.ndarray) -> np.ndarray:
    if not name:
        return np.zeros(c_out, np.int32)
    expect = s_in * (s_w if s_w.size > 1 else np.full(c_out, s_w[0]))
    if name in o.const:  # float bias
        b = np.asarray(o.const[name], np.float64) / expect
        return np.where(b >= 0, np.floor(b + 0.5), np.ceil(b - 0.5)).astype(np.int32)
    b, s_b, z_b, _ = o.const_dq(name)
    if b.dtype != np.int32 or np.any(z_b != 0):
        raise ValueError(f"{name}: bias must be int32 with zero point 0")
    if not np.allclose(s_b, expect, rtol=1e-5):
        raise ValueError(f"{name}: bias scale != input scale × weight scale")
    return b.astype(np.int32)


def _pads_attr(o: _Onnx, node, h_in, w_in, h_out, w_out, kh, kw, stride) -> tuple[int, int, int, int]:
    auto = o.attr(node, "auto_pad", "NOTSET")
    if auto in ("SAME_UPPER", "SAME_LOWER"):
        pt, pb = same_pad(h_in, h_out, kh, stride)
        pl, pr = same_pad(w_in, w_out, kw, stride)
        return (pb, pr, pt, pl) if auto == "SAME_LOWER" else (pt, pl, pb, pr)
    if auto == "VALID":
        return 0, 0, 0, 0
    p = list(o.attr(node, "pads", [0, 0, 0, 0]))
    return p[0], p[1], p[2], p[3]


def _stride(o: _Onnx, node) -> int:
    s = list(o.attr(node, "strides", [1, 1]))
    if s[0] != s[1]:
        raise ValueError(f"{node.name}: non-square stride is unsupported")
    if any(d != 1 for d in o.attr(node, "dilations", [1, 1])):
        raise ValueError(f"{node.name}: dilation is unsupported")
    return int(s[0])


def build_model(name: str, path: Path) -> Model:
    o = _Onnx(path)
    ids: dict[str, int] = {}
    qp: dict[str, tuple[float, int, bool]] = {}   # quantized tensor → (scale, zp, uint8)
    alias: dict[str, str] = {}                     # Q output after Flatten etc. → original quantized tensor
    pads: dict[str, tuple[str, tuple[int, int, int, int], int]] = {}  # Q output after Pad → (input, pad, value)
    done: set[str] = set()                          # Q nodes used as layer outputs
    layers: list[Layer] = []
    first_input = None

    def tid(n: str) -> int:
        return ids.setdefault(n, len(ids))

    def canon(q: str) -> str:
        while q in alias:
            q = alias[q]
        return q

    def pad_of(node, q: str) -> tuple[tuple[int, int, int, int], int]:
        if o.attr(node, "mode", "constant") != "constant":
            raise ValueError(f"{node.name}: only constant Pad is supported")
        p = np.asarray(o.const[node.input[1]]).ravel().tolist() if len(node.input) > 1 else o.attr(node, "pads")
        if len(p) != 8 or p[0] or p[1] or p[4] or p[5]:
            raise ValueError(f"{node.name}: Pad on batch/channel is unsupported")
        v = float(np.asarray(o.const[node.input[2]]).ravel()[0]) if len(node.input) > 2 and node.input[2] else 0.0
        s, zp, _ = qp[q]
        return (int(p[2]), int(p[3]), int(p[6]), int(p[7])), zp + _round_half_away(v / s)

    def act_in(fname: str) -> tuple[str, tuple | None]:
        """Float input → (quantized tensor, pad to fold or None)."""
        n = o.prod.get(fname)
        if n is None:
            raise ValueError(f"{fname}: activation is not quantized")
        if n.op_type == "DequantizeLinear":
            q = canon(n.input[0])
            if q in pads:
                src, p, v = pads[q]
                return src, (p, v)
            if q not in qp:
                raise ValueError(f"{fname}: unknown quantized tensor {q}")
            return q, None
        if n.op_type in _PASS:
            q, p = act_in(n.input[0])
            if p is not None or int(np.prod(o.hwc(q))) != o.hwc(q)[2]:
                raise ValueError(f"{n.name}: {n.op_type} is only supported on 1x1xC tensors")
            return q, None
        if n.op_type == "Pad":
            q, p = act_in(n.input[0])
            if p is not None:
                raise ValueError(f"{n.name}: consecutive Pad is unsupported")
            return q, pad_of(n, q)
        raise ValueError(f"{fname}: unsupported producer {n.op_type}")

    def out_q(fname: str) -> tuple[str, int, int]:
        """Compute op output → (Q output, act_min, act_max). Relu/Clip in between fold into the clamp."""
        cur, acts = fname, []
        while True:
            cs = o.cons.get(cur, [])
            if len(cs) != 1:
                raise ValueError(f"{cur}: output must feed exactly one QuantizeLinear")
            c = cs[0]
            if c.op_type == "QuantizeLinear":
                break
            if c.op_type == "Relu":
                acts.append((0.0, None))
            elif c.op_type == "Clip":
                lo = o.const[c.input[1]] if len(c.input) > 1 and c.input[1] else o.attr(c, "min", None)
                hi = o.const[c.input[2]] if len(c.input) > 2 and c.input[2] else o.attr(c, "max", None)
                acts.append((None if lo is None else float(np.ravel(lo)[0]), None if hi is None else float(np.ravel(hi)[0])))
            else:
                raise ValueError(f"{cur}: unsupported op {c.op_type} after compute op")
            cur = c.output[0]
        done.add(c.output[0])
        q = c.output[0]
        qp[q] = o.qparams(c)
        s, zp, _ = qp[q]
        lo_q, hi_q = -128, 127
        for lo, hi in acts:
            if lo is not None:
                lo_q = max(lo_q, zp + _round_half_away(lo / s))
            if hi is not None:
                hi_q = min(hi_q, zp + _round_half_away(hi / s))
        return q, lo_q, hi_q

    for node in o.g.node:
        op = node.op_type
        if op in ("DequantizeLinear", "Constant", "Softmax", "Pad", *_PASS):
            continue  # interpreted by the consumer
        if op == "QuantizeLinear":
            q = node.output[0]
            if q in done:
                continue
            x = node.input[0]
            if x == o.graph_in:
                if first_input is not None:
                    raise ValueError("graph input is quantized twice")
                qp[q] = o.qparams(node)
                first_input = q
                continue
            pnode = o.prod.get(x)
            if pnode is not None and pnode.op_type == "Softmax":
                continue
            src, p = act_in(x)
            qp[q] = o.qparams(node)
            if qp[q][:2] != qp[src][:2]:
                raise ValueError(f"{node.name}: requantization after {pnode.op_type if pnode else '?'} is unsupported")
            if p is None:
                alias[q] = src
            else:
                pads[q] = (src, *p)
            continue

        if op not in ("Conv", "Gemm", "Add", "MaxPool", "GlobalAveragePool", "AveragePool", "ReduceMean"):
            raise ValueError(f"unsupported op {op} ({node.name})")
        x, fold = act_in(node.input[0])
        h_in, w_in, c_in = o.hwc(x)
        s_in, zp_in, _ = qp[x]
        y, act_min, act_max = out_q(node.output[0])
        h_out, w_out, c_out = o.hwc(y)
        s_out, zp_out, _ = qp[y]
        lay = Layer(
            index=len(layers), name=node.name or y, kind="", inputs=[tid(x)], output=tid(y),
            h_in=h_in, w_in=w_in, c_in=c_in, h_out=h_out, w_out=w_out, c_out=c_out,
            zp_in=zp_in, zp_out=zp_out, scale_in=s_in, scale_out=s_out, op=op,
            act_min=act_min, act_max=act_max,
        )
        fp, fv = fold if fold is not None else ((0, 0, 0, 0), None)

        if op == "Conv":
            group = int(o.attr(node, "group", 1))
            w, s_w = _weights(o, node.input[1], c_out, 0)  # [Cout, Cin/g, kh, kw]
            kh, kw = int(w.shape[2]), int(w.shape[3])
            stride = _stride(o, node)
            if group == 1:
                lay.kind = "CONV"
                w4 = np.ascontiguousarray(w.transpose(0, 2, 3, 1))  # [Cout, kh, kw, Cin]
            elif group == c_in == c_out and w.shape[1] == 1:
                lay.kind = "DWCONV"
                w4 = np.ascontiguousarray(w.transpose(0, 2, 3, 1))  # [C, kh, kw, 1] (for coefficient computation)
            else:
                raise ValueError(f"{node.name}: grouped conv (group={group}) is unsupported")
            cp = _pads_attr(o, node, h_in, w_in, h_out, w_out, kh, kw, stride)
            if fv is not None and fv != zp_in:
                raise ValueError(f"{node.name}: folded Pad value differs from conv input zp")
            lay.kh, lay.kw, lay.stride = kh, kw, stride
            lay.pad_t, lay.pad_l, lay.pad_b, lay.pad_r = (a + b for a, b in zip(cp, fp))
            lay.pad_value = zp_in
            bias = _bias(o, node.input[2] if len(node.input) > 2 else None, c_out, s_in, s_w)
            lay.eff_bias, lay.mult = _conv_coeffs(w4, bias, s_in, zp_in, s_w, s_out)
            lay.weights = np.ascontiguousarray(w[:, 0].transpose(1, 2, 0)) if lay.kind == "DWCONV" else w4
        elif op == "Gemm":
            if o.attr(node, "transA", 0) or o.attr(node, "alpha", 1.0) != 1.0 or o.attr(node, "beta", 1.0) != 1.0:
                raise ValueError(f"{node.name}: Gemm with transA/alpha/beta is unsupported")
            if (h_in, w_in) != (1, 1) or fold is not None:
                raise ValueError(f"{node.name}: Gemm input must be 1x1xC")
            tb = int(o.attr(node, "transB", 0))
            w, s_w = _weights(o, node.input[1], c_out, 0 if tb else 1)
            w = w if tb else w.T  # [N, K]
            lay.kind = "CONV"
            w4 = np.ascontiguousarray(w.reshape(c_out, 1, 1, c_in))
            bias = _bias(o, node.input[2] if len(node.input) > 2 else None, c_out, s_in, s_w)
            lay.eff_bias, lay.mult = _conv_coeffs(w4, bias, s_in, zp_in, s_w, s_out)
            lay.weights = w4
            lay.pad_value = zp_in
        elif op == "Add":
            b, fb = act_in(node.input[1])
            if fold is not None or fb is not None or o.hwc(b) != (h_in, w_in, c_in):
                raise ValueError(f"{node.name}: Add with broadcast/pad is unsupported")
            s_b, zp_b, _ = qp[b]
            lay.kind = "ADD"
            lay.inputs = [tid(x), tid(b)]
            lay.zp_in2, lay.scale_in2 = zp_b, s_b
            ra, rb = s_in / s_out, s_b / s_out
            _, sh = mant_shift(max(ra, rb))
            lay.scalar0 = mult_word(scaled(ra, sh), sh)
            lay.scalar1 = scaled(rb, sh)
        elif op == "MaxPool":
            if o.attr(node, "ceil_mode", 0) or o.attr(node, "storage_order", 0):
                raise ValueError(f"{node.name}: MaxPool ceil_mode/storage_order is unsupported")
            if (s_in, zp_in) != (s_out, zp_out):
                raise ValueError(f"{node.name}: MaxPool with requantization is unsupported")
            ks = list(o.attr(node, "kernel_shape"))
            lay.kind = "MAXPOOL"
            lay.kh, lay.kw = int(ks[0]), int(ks[1])
            lay.stride = _stride(o, node)
            cp = _pads_attr(o, node, h_in, w_in, h_out, w_out, lay.kh, lay.kw, lay.stride)
            if fold is not None and any(cp):
                raise ValueError(f"{node.name}: Pad followed by padded MaxPool is unsupported")
            lay.pad_t, lay.pad_l, lay.pad_b, lay.pad_r = fp if fold is not None else cp
            lay.pad_value = fv if fv is not None else -128
        else:  # global average
            if op == "AveragePool":
                if list(o.attr(node, "kernel_shape")) != [h_in, w_in] or any(o.attr(node, "pads", [0])):
                    raise ValueError(f"{node.name}: only global AveragePool is supported")
            elif op == "ReduceMean":
                axes = o.attr(node, "axes", None)
                if axes is None and len(node.input) > 1:
                    axes = np.asarray(o.const[node.input[1]]).ravel().tolist()
                if sorted(int(a) % 4 for a in axes) != [2, 3]:
                    raise ValueError(f"{node.name}: ReduceMean over axes {axes} is unsupported")
            if fold is not None or (h_out, w_out) != (1, 1):
                raise ValueError(f"{node.name}: average pool output must be 1x1")
            n = h_in * w_in
            lay.kind = "AVGPOOL"
            lay.kh, lay.kw = h_in, w_in
            lay.scalar0 = mult_word(*mant_shift(s_in / (s_out * n)))
            lay.scalar1 = -n * zp_in
        layers.append(lay)

    if not layers or first_input is None:
        raise ValueError("empty model or unquantized graph input")
    first = layers[0]
    if first.inputs[0] != tid(first_input):
        raise ValueError("first layer does not consume the graph input")
    first.in_dense = first.c_in < 8
    s_i, z_i, _ = qp[first_input]
    c_cls = layers[-1].c_out
    return Model(
        name=name, layers=layers, input_id=tid(first_input),
        input_shape=(first.h_in, first.w_in, first.c_in), input_scale=s_i, input_zp=z_i,
        output_id=layers[-1].output, num_classes=c_cls, class_offset=c_cls - 1000,
        onnx_path=Path(path), tensor_names={v: k for k, v in ids.items()},
        uint8_tensors=frozenset(ids[q] for q, v in qp.items() if v[2] and q in ids),
    )
