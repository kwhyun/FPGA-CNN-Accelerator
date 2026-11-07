"""Golden vs onnxruntime: per-layer output differences (one image).

    python -m compiler.golden.compare_ref --model resnet50 [--image 1]
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from compiler.golden.layers import run_layer
from compiler.golden.onnx_ref import OnnxRef
from compiler.model.config import model_build_dir, model_config
from compiler.model.inputs import load_image
from compiler.model.ir import load_model


def compare(name: str, image: int) -> list[dict]:
    model = load_model(name)
    ref = OnnxRef(model)
    x_ref, x_acc = ref.inputs(load_image(model_config(name), image))
    ref.run(x_ref)
    if not np.array_equal(ref.tensor(model.input_id), x_acc):
        raise AssertionError("first-layer input differs from onnxruntime")
    gold = {model.input_id: x_acc}
    rows = []
    for lay in model.layers:
        gold[lay.output] = run_layer(lay, *[gold[i] for i in lay.inputs])
        t = ref.tensor(lay.output).astype(np.int32).reshape(-1)
        d = gold[lay.output].astype(np.int32).reshape(-1) - t
        # standalone: arithmetic difference when fed the onnxruntime input
        di = run_layer(lay, *[ref.tensor(i) for i in lay.inputs]).astype(np.int32).reshape(-1) - t
        rows.append({
            "layer": lay.index, "kind": lay.kind, "op": lay.op,
            "chain_max_abs": int(np.abs(d).max()), "chain_frac_ne": float((d != 0).mean()),
            "iso_max_abs": int(np.abs(di).max()), "iso_frac_ne": float((di != 0).mean()),
            "iso_mean": float(di.mean()),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--image", type=int, default=1)
    args = ap.parse_args()
    rows = compare(args.model, args.image)
    out = model_build_dir(args.model) / "layer_diff.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": args.model, "image": args.image, "layers": rows}, indent=1))
    worst = sorted(rows, key=lambda r: (-r["iso_max_abs"], -r["iso_frac_ne"]))[:4]
    print(f"{args.model}: layers={len(rows)} logits chain max|d|={rows[-1]['chain_max_abs']}; isolated worst: " +
          ", ".join(f"L{r['layer']}({r['op']}) max {r['iso_max_abs']} ne {r['iso_frac_ne']:.4f}" for r in worst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
