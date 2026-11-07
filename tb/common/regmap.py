"""# Auto-generated from data/regmap.yaml by compiler/regmap/generate.py.
# DO NOT EDIT BY HAND — regenerate via `python -m compiler.regmap.generate`.
"""
from __future__ import annotations

from enum import IntEnum

ADDRESS_WIDTH = 12
DATA_WIDTH    = 32

class Reg(IntEnum):
    CTRL = 0x00
    STATUS = 0x04
    IRQ_EN = 0x08
    IRQ_STATUS = 0x0C
    INSTR_BASE = 0x10
    INSTR_COUNT = 0x14
    LAYER_INDEX = 0x18
    ERROR_CODE = 0x1C
    PERF_SELECT = 0x20
    PERF_DATA = 0x24
    HW_VERSION = 0x28

class CtrlBit(IntEnum):
    START = 0
    SOFT_RESET = 1

class StatusBit(IntEnum):
    BUSY = 0
    DONE = 1
    ERROR = 2

class IrqEnBit(IntEnum):
    DONE = 0
    ERROR = 1

class IrqStatusBit(IntEnum):
    DONE = 0
    ERROR = 1
