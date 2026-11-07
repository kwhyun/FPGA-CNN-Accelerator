"""ImageNet val Top-1/Top-5: integer golden vs onnxruntime.

    python -m compiler.golden.accuracy --model resnet50 --n 1000 [--start 1] [--jobs 8]
"""
from __future__ import annotations

import argparse
import json
import os
from multiprocessing import Pool

import numpy as np

from compiler.model.config import model_build_dir, model_config
from compiler.model.images import label
from compiler.model.inputs import load_image

_STATE: dict = {}


def _init(name: str) -> None:
    from compiler.golden.onnx_ref import OnnxRef
    from compiler.model.ir import load_model

    model = load_model(name)
    _STATE["model"] = model
    _STATE["ref"] = OnnxRef(model, threads=1)


def _one(index: int) -> tuple[int, int, list[int], list[int]]:
    from compiler.golden.run import logits, topk

    model, ref = _STATE["model"], _STATE["ref"]
    x_ref, x_acc = ref.inputs(load_image(model_config(model.name), index))
    ref.run(x_ref)
    off = model.class_offset
    t5 = [int(c) - off for c in topk(ref.logits(), 5)]
    g5 = [int(c) - off for c in topk(logits(model, x_acc), 5)]
    return index, label(index), g5, t5


def evaluate(name: str, start: int, n: int, jobs: int) -> dict:
    from compiler.model.ir import load_model

    load_model(name)  # quantize once in the parent process if needed
    with Pool(jobs, initializer=_init, initargs=(name,)) as pool:
        rows = sorted(pool.map(_one, range(start, start + n), chunksize=4))
    lab = np.array([r[1] for r in rows])
    g = np.array([r[2] for r in rows])
    t = np.array([r[3] for r in rows])
    res = {
        "model": name, "start": start, "n": n,
        "golden_top1": float((g[:, 0] == lab).mean()), "golden_top5": float((g == lab[:, None]).any(1).mean()),
        "onnx_top1": float((t[:, 0] == lab).mean()), "onnx_top5": float((t == lab[:, None]).any(1).mean()),
        "top1_agreement": float((g[:, 0] == t[:, 0]).mean()),
        "per_image": [{"index": r[0], "label": r[1], "golden_top5": r[2], "onnx_top5": r[3]} for r in rows],
    }
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = ap.parse_args()
    res = evaluate(args.model, args.start, args.n, args.jobs)
    out = model_build_dir(args.model) / f"accuracy_n{args.n}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    print(f"{args.model} n={args.n}: golden top1 {res['golden_top1']:.2%} top5 {res['golden_top5']:.2%} | "
          f"onnxruntime top1 {res['onnx_top1']:.2%} top5 {res['onnx_top5']:.2%} | top1 agree {res['top1_agreement']:.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
