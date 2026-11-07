"""onnxruntime reference runner: returns every quantized tensor (layer inputs/outputs) as an output."""
from __future__ import annotations

import numpy as np

from compiler.model.config import model_config
from compiler.model.inputs import first_layer_input, onnx_input
from compiler.model.ir import Model


class OnnxRef:
    def __init__(self, model: Model, threads: int = 0):
        import onnx
        import onnxruntime as ort

        self.model = model
        m = onnx.load(str(model.onnx_path))
        have = {o.name for o in m.graph.output}
        for name in model.tensor_names.values():
            if name not in have:
                m.graph.output.append(onnx.ValueInfoProto(name=name))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(m.SerializeToString(), opts, providers=["CPUExecutionProvider"])
        self.in_name = self.sess.get_inputs()[0].name
        self.out_names = [o.name for o in self.sess.get_outputs()]
        self.cfg = model_config(model.name)
        self.out: dict[str, np.ndarray] = {}

    def inputs(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(ONNX float input, accelerator first-layer int8 input)."""
        return onnx_input(self.cfg, rgb), first_layer_input(self.model, rgb)

    def run(self, x: np.ndarray) -> None:
        self.out = dict(zip(self.out_names, self.sess.run(self.out_names, {self.in_name: x})))

    def tensor(self, tid: int) -> np.ndarray:
        """IR tensor id → int8 [H, W, C]."""
        v = self.out[self.model.tensor_names[tid]]
        if tid in self.model.uint8_tensors:
            v = v.astype(np.int16) - 128
        v = v.astype(np.int8)
        return v.reshape(1, 1, -1) if v.ndim == 2 else v[0].transpose(1, 2, 0)

    def logits(self) -> np.ndarray:
        return self.tensor(self.model.output_id).reshape(-1)
