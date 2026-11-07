"""Board flow on the TLM: model.bin + images.bin, patch instr0.in_base per image, logits == golden."""
from __future__ import annotations

import re

import numpy as np

from compiler.analysis.tlm import Ddr, run_instr
from compiler.codegen.export_board import export
from compiler.codegen.isa import Instr
from compiler.codegen.program import DDR_BASE
from compiler.golden.run import logits
from compiler.model.config import model_config
from compiler.model.inputs import first_layer_input, load_image
from compiler.model.ir import load_model


def test_board_flow(tmp_path):
    name, n = "mobilenet_v2", 2
    export(name, n, 1, tmp_path)
    hdr = (tmp_path / "board_model.h").read_text()
    val = lambda k: int(re.search(rf"#define {k} (0x[0-9A-F]+|\d+)u", hdr).group(1), 0)
    model = load_model(name)
    cfg = model_config(name)
    images = (tmp_path / "images.bin").read_bytes()
    stride, img_base = val("IMAGE_STRIDE"), val("IMAGES_DDR_BASE")
    for i in range(n):
        ddr = Ddr({DDR_BASE: (tmp_path / "model.bin").read_bytes(),
                   img_base: images[: n * stride]}, size=(img_base - DDR_BASE) + n * stride)
        ddr.write(val("INSTR0_IN_BASE_ADDR"), (img_base + i * stride).to_bytes(4, "little"))
        blob = ddr.read(val("INSTR_ADDR"), 64 * val("INSTR_COUNT"))
        for k in range(val("INSTR_COUNT")):
            run_instr(ddr, Instr.decode(blob[64 * k: 64 * k + 64]))
        got = np.frombuffer(ddr.read(val("LOGITS_ADDR"), -(-val("NUM_CLASSES") // 8) * 8), np.int8)[: val("NUM_CLASSES")]
        x = first_layer_input(model, load_image(cfg, 1 + i))
        np.testing.assert_array_equal(got, logits(model, x))
