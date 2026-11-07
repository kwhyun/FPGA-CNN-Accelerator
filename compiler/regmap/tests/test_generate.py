"""regmap: generated files match data/regmap.yaml; invalid specs are rejected."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from compiler.regmap.generate import (
    C_PATHS,
    PY_PATH,
    SVH_PATH,
    load_regmap,
    render_c,
    render_py,
    render_svh,
)


def test_generated_files_are_current() -> None:
    spec = load_regmap()
    assert SVH_PATH.read_text() == render_svh(spec)
    assert PY_PATH.read_text() == render_py(spec)
    for path in C_PATHS:
        assert path.read_text() == render_c(spec)


def test_control_registers_present() -> None:
    regs = {r.name: r for r in load_regmap().registers}
    assert regs["CTRL"].offset == 0x00
    assert {f.name: f.access for f in regs["CTRL"].fields} == {"START": "w1p", "SOFT_RESET": "w1p"}
    assert regs["IRQ_STATUS"].access == "w1c"
    assert regs["INSTR_BASE"].width == 32


@pytest.mark.parametrize(
    "body",
    [
        # duplicate offset
        """
        - {name: A, offset: 0x0, access: rw, width: 32}
        - {name: B, offset: 0x0, access: rw, width: 32}
        """,
        # duplicate bit
        """
        - name: A
          offset: 0x0
          access: rw
          fields: [{name: X, bit: 0}, {name: Y, bit: 0}]
        """,
        # bad access
        """
        - {name: A, offset: 0x0, access: wx, width: 32}
        """,
        # unaligned
        """
        - {name: A, offset: 0x2, access: rw, width: 32}
        """,
    ],
)
def test_invalid_spec_rejected(tmp_path: Path, body: str) -> None:
    text = "address_width: 12\ndata_width: 32\nregisters:\n" + textwrap.indent(
        textwrap.dedent(body).strip("\n"), "  "
    )
    path = tmp_path / "regmap.yaml"
    path.write_text(text + "\n")
    with pytest.raises(ValueError):
        load_regmap(path)
