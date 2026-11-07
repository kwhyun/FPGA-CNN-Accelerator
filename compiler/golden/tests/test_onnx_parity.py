"""Golden layers fed with onnxruntime tensors stay within 1 LSB of onnxruntime (one image)."""
from __future__ import annotations

import numpy as np
import pytest

from compiler.golden.layers import run_layer
from compiler.golden.onnx_ref import OnnxRef
from compiler.model.config import model_config
from compiler.model.inputs import load_image
from compiler.model.ir import load_model


@pytest.mark.parametrize("name", ["efficientnet-lite0", "mobilenet_v2", "resnet50"])
def test_isolated_layers_within_one_lsb(name):
    model = load_model(name)
    ref = OnnxRef(model)
    x_ref, x_acc = ref.inputs(load_image(model_config(name), 1))
    ref.run(x_ref)
    np.testing.assert_array_equal(ref.tensor(model.input_id), x_acc)
    for lay in model.layers:
        ops = [ref.tensor(i) for i in lay.inputs]
        d = run_layer(lay, *ops).astype(np.int32).reshape(-1) - ref.tensor(lay.output).astype(np.int32).reshape(-1)
        assert np.abs(d).max() <= 1, lay.index
        assert (d != 0).mean() < 1e-3, lay.index
