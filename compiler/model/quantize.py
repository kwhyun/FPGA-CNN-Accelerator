"""float32 ONNX → INT8 QDQ ONNX (onnxruntime static quantization).

Activations asymmetric int8 (per-tensor), weights symmetric int8 (per-channel), MinMax calibration.
Calibration uses ``calibration_images`` ImageNet val images from 40001 (no overlap with the evaluation range 1..).
Models that are already QDQ are used as is.
"""
from __future__ import annotations

import numpy as np
import onnx

from compiler.model.config import ModelConfig, model_build_dir, model_config
from compiler.model.inputs import load_image, onnx_input

CALIB_START = 40001


def is_quantized(path) -> bool:
    g = onnx.load(str(path), load_external_data=False).graph
    return any(n.op_type in ("QuantizeLinear", "DequantizeLinear", "QLinearConv") for n in g.node)


class _Reader:
    def __init__(self, cfg: ModelConfig, input_name: str):
        self.cfg, self.input_name = cfg, input_name
        self.it = iter(range(CALIB_START, CALIB_START + cfg.calibration_images))

    def get_next(self):
        i = next(self.it, None)
        if i is None:
            return None
        return {self.input_name: onnx_input(self.cfg, load_image(self.cfg, i))}

    def rewind(self):
        self.it = iter(range(CALIB_START, CALIB_START + self.cfg.calibration_images))


def quantized_path(name: str):
    """Path to the INT8 QDQ ONNX. Built if needed (only once, even with concurrent processes)."""
    import fcntl
    import os

    cfg = model_config(name)
    if is_quantized(cfg.onnx):
        return cfg.onnx
    out_dir = model_build_dir(name)
    out = out_dir / f"{name}_int8.onnx"
    src_mtime = max(cfg.onnx.stat().st_mtime, cfg.onnx.with_suffix(".yaml").stat().st_mtime)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / ".quantize.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if out.exists() and out.stat().st_mtime >= src_mtime:
            return out
        from onnxruntime.quantization import CalibrationMethod, QuantFormat, QuantType, quantize_static
        from onnxruntime.quantization.shape_inference import quant_pre_process

        pre = out_dir / f"{name}_pre.onnx"
        tmp = out_dir / f"{name}_int8.tmp.onnx"
        quant_pre_process(str(cfg.onnx), str(pre), skip_symbolic_shape=True)
        input_name = onnx.load(str(pre), load_external_data=False).graph.input[0].name
        print(f"{name}: calibrating on {cfg.calibration_images} images", flush=True)
        quantize_static(
            str(pre), str(tmp), _Reader(cfg, input_name),
            quant_format=QuantFormat.QDQ, per_channel=True,
            activation_type=QuantType.QInt8, weight_type=QuantType.QInt8,
            calibrate_method=CalibrationMethod.MinMax,
            extra_options={"ActivationSymmetric": False, "WeightSymmetric": True},
        )
        pre.unlink()
        os.replace(tmp, out)
    return out
