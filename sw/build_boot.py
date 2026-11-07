"""firmware ELF + BOOT.bin (FSBL, bitstream, firmware, model.bin, images.bin).

    python sw/build_boot.py --model resnet50

Requires the make hw outputs (build/hw/accel_hw.xsa, build/hw/platform/) and the
compiler.codegen.export_board outputs (build/<model>/board/). Vitis tools (arm-none-eabi-gcc, bootgen) must be on PATH.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HW = ROOT / "build" / "hw"
PLATFORM = HW / "platform" / "accel_platform" / "export" / "accel_platform" / "sw"
BSP = PLATFORM / "standalone_ps7_cortexa9_0"
FSBL = PLATFORM / "boot" / "fsbl.elf"
LSCRIPT = ROOT / "sw" / "src" / "lscript.ld"


def tool(name: str) -> str:
    p = shutil.which(name)
    if p is None:
        raise FileNotFoundError(f"{name} not in PATH (source Vitis settings64.sh)")
    return p


def build_app(model_dir: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for name in ("main.c", "accel_regs.h"):
        shutil.copyfile(ROOT / "sw/src/accel" / name, out / name)
    shutil.copyfile(model_dir / "board_model.h", out / "board_model.h")
    gcc = tool("arm-none-eabi-gcc")
    flags = ["-DSDT", "-mcpu=cortex-a9", "-mfpu=vfpv3", "-mfloat-abi=hard", "-O2", "-Wall",
             "-specs=" + str(BSP / "Xilinx.spec"), "-I" + str(out), "-I" + str(BSP / "include")]
    obj = out / "main.o"
    subprocess.run([gcc, *flags, "-c", str(out / "main.c"), "-o", str(obj)], check=True)
    elf = out / "accel_fw.elf"
    subprocess.run([gcc, *flags, str(obj), "-Wl,-T," + str(LSCRIPT), "-L" + str(BSP / "lib"),
                    "-Wl,--start-group", "-lxilstandalone", "-lxiltimer", "-lxil", "-lgcc", "-lc",
                    "-Wl,--end-group", "-o", str(elf)], check=True)
    return elf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--xsa", type=Path, default=HW / "accel_hw.xsa")
    args = ap.parse_args()
    for p in (args.xsa, FSBL, BSP):
        if not p.exists():
            raise FileNotFoundError(f"{p} not found (run make hw)")
    build = ROOT / "build" / args.model
    model_dir = build / "board"
    work = build / "boot"
    work.mkdir(parents=True, exist_ok=True)
    app = build_app(model_dir, work / "app")
    with zipfile.ZipFile(args.xsa) as z:
        bit_name = next(n for n in z.namelist() if n.endswith(".bit"))
        (work / "accel.bit").write_bytes(z.read(bit_name))
    meta = json.loads((model_dir / "manifest.json").read_text())
    bif = work / "boot.bif"
    bif.write_text("the_ROM_image:\n{\n"
                   f"  [bootloader] {FSBL}\n  {work / 'accel.bit'}\n  {app}\n"
                   f"  [load=0x{meta['model_ddr_base']:08X}] {model_dir / 'model.bin'}\n"
                   f"  [load=0x{meta['images_ddr_base']:08X}] {model_dir / 'images.bin'}\n}}\n")
    boot = build / "BOOT.bin"
    subprocess.run([tool("bootgen"), "-image", str(bif), "-arch", "zynq", "-o", str(boot), "-w", "on"],
                   check=True, stdout=subprocess.DEVNULL)
    info = {"model": args.model, "xsa_sha256": hashlib.sha256(args.xsa.read_bytes()).hexdigest(),
            "boot_sha256": hashlib.sha256(boot.read_bytes()).hexdigest(),
            "app_sha256": hashlib.sha256(app.read_bytes()).hexdigest(), **meta}
    (build / "boot_manifest.json").write_text(json.dumps(info, indent=1))
    print(boot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
