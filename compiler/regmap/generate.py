"""Regmap generator — yaml → SVH + Python IntEnum.

CLI: ``python -m compiler.regmap.generate``  (project root).

The data model maps 1:1 to the yaml schema (Field / Register / RegmapSpec dataclasses).
``render_svh`` / ``render_py`` are pure functions: the same spec always yields the same
string. ``main`` writes the results to ``rtl/accel_regs.svh`` / ``tb/common/regmap.py``,
skipping the write when the existing file already matches (keeps mtime stable).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # PyYAML: YAML → Python dict/list loader


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT  = Path(__file__).resolve().parents[2]  # two levels above compiler/regmap/ = repository root
YAML_PATH  = REPO_ROOT / "data" / "regmap.yaml"
SVH_PATH   = REPO_ROOT / "rtl" / "accel_regs.svh"
PY_PATH    = REPO_ROOT / "tb" / "common" / "regmap.py"
# Each Vitis app project keeps its own copy of the C header (each project's src/ builds independently).
# All copies must be identical; the list below is where the single source is distributed.
C_PATHS = (REPO_ROOT / "sw/src/accel/accel_regs.h",)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
# Allowed access strings (fast membership check).
_FIELD_ACCESS  = {"rw", "ro", "w1p", "w1c"}
_REG_ACCESS    = {"rw", "ro", "w1c"}


@dataclass(frozen=True)
class Field:
    name: str
    bit: int
    access: str  # rw / ro / w1p / w1c


@dataclass(frozen=True)
class Register:
    name: str
    offset: int
    access: str
    # `= ()` empty-tuple default: tuples are immutable, so this is safe for a frozen dataclass.
    # (A mutable default would need field(default_factory=list) to avoid shared state.)
    fields: tuple[Field, ...] = ()
    width: int | None = None  # set when register is full-word scalar (no bit fields)


@dataclass(frozen=True)
class RegmapSpec:
    address_width: int
    data_width: int
    registers: tuple[Register, ...]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def load_regmap(yaml_path: Path = YAML_PATH) -> RegmapSpec:
    # safe_load: YAML parser that refuses to build arbitrary Python objects.
    raw = yaml.safe_load(yaml_path.read_text())

    address_width = int(raw["address_width"])
    data_width    = int(raw["data_width"])
    if data_width != 32:
        raise ValueError(f"only data_width=32 supported, got {data_width}")
    if address_width < 4 or address_width > 32:
        raise ValueError(f"address_width out of range: {address_width}")

    regs: list[Register] = []
    seen_offsets: set[int] = set()  # for duplicate check
    seen_names:   set[str] = set()
    for raw_reg in raw["registers"]:
        name   = str(raw_reg["name"])
        offset = int(raw_reg["offset"])
        access = str(raw_reg["access"])
        if name in seen_names:
            raise ValueError(f"duplicate register name: {name}")
        if offset in seen_offsets:
            raise ValueError(f"duplicate register offset: 0x{offset:x}")
        if offset & 0x3:  # 4 B alignment (low 2 bits = 0)
            raise ValueError(f"register {name} offset 0x{offset:x} not 4 B aligned")
        if offset >= (1 << address_width):  # beyond address space
            raise ValueError(
                f"register {name} offset 0x{offset:x} exceeds address_width={address_width}"
            )
        if access not in _REG_ACCESS:
            raise ValueError(f"register {name} access {access!r} not in {_REG_ACCESS}")

        # fields / width are optional keys (.get → None if absent). Exactly one must be present.
        fields_raw = raw_reg.get("fields")
        width      = raw_reg.get("width")
        if fields_raw and width:
            raise ValueError(f"register {name}: cannot set both `fields` and `width`")
        if fields_raw:
            fields_tuple = _parse_fields(name, fields_raw, data_width, default_access=access)
            reg = Register(name=name, offset=offset, access=access, fields=fields_tuple)
        elif width:
            width_int = int(width)
            if width_int != data_width:
                raise ValueError(
                    f"register {name} width={width_int} != data_width={data_width}"
                )
            reg = Register(name=name, offset=offset, access=access, width=width_int)
        else:
            raise ValueError(f"register {name}: must set either `fields` or `width`")

        regs.append(reg)
        seen_offsets.add(offset)
        seen_names.add(name)

    return RegmapSpec(
        address_width=address_width,
        data_width=data_width,
        registers=tuple(regs),
    )


def _parse_fields(
    reg_name: str,
    raw_fields: list[dict],
    data_width: int,
    default_access: str,
) -> tuple[Field, ...]:
    fields: list[Field] = []
    seen_bits:  set[int] = set()
    seen_names: set[str] = set()
    for raw_f in raw_fields:
        name = str(raw_f["name"])
        bit  = int(raw_f["bit"])
        acc  = str(raw_f.get("access", default_access))  # inherit register access if absent
        if name in seen_names:
            raise ValueError(f"{reg_name}: duplicate field name {name}")
        if bit in seen_bits:
            raise ValueError(f"{reg_name}: duplicate bit position {bit}")
        if bit < 0 or bit >= data_width:
            raise ValueError(f"{reg_name}.{name}: bit {bit} out of [0,{data_width})")
        if acc not in _FIELD_ACCESS:
            raise ValueError(f"{reg_name}.{name}: access {acc!r} not in {_FIELD_ACCESS}")
        fields.append(Field(name=name, bit=bit, access=acc))
        seen_bits.add(bit)
        seen_names.add(name)
    return tuple(fields)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
_HEADER_LINE = (
    "// Auto-generated from data/regmap.yaml by compiler/regmap/generate.py.\n"
    "// DO NOT EDIT BY HAND — regenerate via `python -m compiler.regmap.generate`.\n"
)


def render_svh(spec: RegmapSpec) -> str:
    aw = spec.address_width
    lines: list[str] = []
    lines.append(_HEADER_LINE.rstrip("\n"))
    lines.append("")
    lines.append(f"// AXI-Lite: {aw}-bit address, {spec.data_width}-bit data.")
    lines.append("")
    lines.append("// ----- Register offsets -----")
    for reg in spec.registers:
        # Dynamic-width format: the inner `{(aw+3)//4}` in `{offset:0{width}X}` computes the
        # number of hex digits, used as the zero-padded width (nested format spec).
        lines.append(
            f"localparam logic [{aw - 1}:0] {reg.name}_OFFSET = "
            f"{aw}'h{reg.offset:0{(aw + 3) // 4}X};"
        )
    lines.append("")
    lines.append("// ----- Field bit positions -----")
    for reg in spec.registers:
        if not reg.fields:
            continue
        for f in reg.fields:
            lines.append(
                f"localparam int unsigned {reg.name}_{f.name}_BIT = {f.bit};"
            )
    lines.append("")
    return "\n".join(lines)


def render_py(spec: RegmapSpec) -> str:
    parts: list[str] = []
    # docstring for the generated .py: comment marker // → #.
    parts.append('"""' + _HEADER_LINE.replace("//", "#") + '"""')
    parts.append("from __future__ import annotations")
    parts.append("")
    parts.append("from enum import IntEnum")
    parts.append("")
    parts.append(f"ADDRESS_WIDTH = {spec.address_width}")
    parts.append(f"DATA_WIDTH    = {spec.data_width}")
    parts.append("")
    parts.append("class Reg(IntEnum):")
    for reg in spec.registers:
        parts.append(f"    {reg.name} = 0x{reg.offset:02X}")
    parts.append("")
    for reg in spec.registers:
        if not reg.fields:
            continue
        parts.append(f"class {_camel_case(reg.name)}Bit(IntEnum):")
        for f in reg.fields:
            parts.append(f"    {f.name} = {f.bit}")
        parts.append("")
    return "\n".join(parts)


def render_c(spec: RegmapSpec) -> str:
    guard = "ACCEL_REGS_H"  # include guard macro
    lines: list[str] = []
    lines.append(_HEADER_LINE.rstrip("\n"))
    lines.append("")
    lines.append(f"#ifndef {guard}")
    lines.append(f"#define {guard}")
    lines.append("")
    lines.append(f"// AXI-Lite: {spec.address_width}-bit address, {spec.data_width}-bit data.")
    lines.append("// Offsets are byte offsets from the accelerator AXI-Lite base")
    lines.append("// (XPAR_ACCEL_TOP_0_BASEADDR).")
    lines.append("")
    lines.append("// ----- Register offsets -----")
    for reg in spec.registers:
        lines.append(f"#define ACCEL_{reg.name}_OFFSET 0x{reg.offset:02X}u")  # u = C unsigned
    lines.append("")
    lines.append("// ----- Field bit positions / masks -----")
    for reg in spec.registers:
        if not reg.fields:
            continue
        for f in reg.fields:
            lines.append(f"#define ACCEL_{reg.name}_{f.name}_BIT {f.bit}u")
            lines.append(
                f"#define ACCEL_{reg.name}_{f.name}_MASK "
                f"(1u << ACCEL_{reg.name}_{f.name}_BIT)"
            )
    lines.append("")
    lines.append(f"#endif  // {guard}")
    lines.append("")
    return "\n".join(lines)


def _camel_case(name: str) -> str:
    # snake_case → CamelCase (split on underscores, capitalize each part, join).
    return "".join(part.capitalize() for part in name.split("_"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    spec = load_regmap()
    svh_text = render_svh(spec)
    py_text  = render_py(spec)
    c_text   = render_c(spec)
    # Overwrite only files whose content changed (avoids needless mtime updates).
    _write_if_changed(SVH_PATH, svh_text)
    _write_if_changed(PY_PATH,  py_text)
    for c_path in C_PATHS:
        _write_if_changed(c_path, c_text)
    return 0


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text() == content:
        return  # same content → skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
