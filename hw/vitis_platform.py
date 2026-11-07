"""XSA → Vitis standalone platform (BSP + FSBL).

    vitis -s hw/vitis_platform.py build/hw/accel_hw.xsa build/hw/platform
Outputs: <ws>/accel_platform/export/accel_platform/sw/{boot/fsbl.elf, standalone_ps7_cortexa9_0/}
"""
import shutil
import sys
from pathlib import Path

import vitis

xsa, ws = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
if ws.exists():
    shutil.rmtree(ws)
ws.mkdir(parents=True)
client = vitis.create_client()
client.set_workspace(path=str(ws))
platform = client.create_platform_component(
    name="accel_platform", hw_design=str(xsa), os="standalone", cpu="ps7_cortexa9_0",
    domain_name="standalone_ps7_cortexa9_0")
platform.build()
vitis.dispose()
