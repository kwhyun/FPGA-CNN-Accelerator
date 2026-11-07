"""cocotb + Verilator runner shared by unit/integration tests.

    from tb.common.sim import run_cocotb
    def test_foo(): run_cocotb("foo_tb", ["rtl/foo.sv", "tb/unit/foo_tb.sv"], "tb.unit.test_foo")
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERILATOR_ARGS = ["-Wno-fatal", "-Wno-WIDTH", "-Wno-UNUSEDPARAM", "-Wno-UNUSEDSIGNAL", "--x-assign", "unique",
                  "--x-initial", "unique"]


def run_cocotb(toplevel: str, sources: list[str | Path], test_module: str, *,
               parameters: dict | None = None, build_name: str | None = None,
               testcase: str | None = None, waves: bool = False) -> None:
    from cocotb_tools.runner import get_runner

    build_dir = ROOT / "sim" / (build_name or toplevel)
    runner = get_runner("verilator")
    runner.build(
        sources=[ROOT / s for s in sources],
        hdl_toplevel=toplevel,
        build_dir=build_dir,
        always=True,
        includes=[str(ROOT / "rtl")],
        parameters=parameters or {},
        build_args=VERILATOR_ARGS + (["--trace-fst"] if waves else []),
        waves=waves,
    )
    env = {"PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    runner.test(
        hdl_toplevel=toplevel,
        test_module=test_module,
        build_dir=build_dir,
        test_dir=build_dir,
        extra_env=env,
        testcase=testcase,
        waves=waves,
        results_xml=str(build_dir / "results.xml"),
    )
