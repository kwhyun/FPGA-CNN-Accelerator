# make hw                 : RTL → bitstream/XSA → Vitis platform (BSP, FSBL)   [build/hw/]
# make boot MODEL=<name>  : data/model/<name>.onnx → INT8 → instructions/DDR image → BOOT.bin   [build/<name>/BOOT.bin]
PY  := .venv/bin/python
VENV := .venv/.installed
N   ?= 1000

.PHONY: hw boot

hw: build/hw/platform/.done

build/hw/accel_hw.xsa: $(wildcard rtl/*.sv rtl/*.v rtl/*.svh) hw/system_impl.tcl
	vivado -mode batch -nojournal -nolog -source hw/system_impl.tcl

build/hw/platform/.done: build/hw/accel_hw.xsa hw/vitis_platform.py
	vitis -s hw/vitis_platform.py build/hw/accel_hw.xsa build/hw/platform
	touch $@

boot: $(VENV) | build/hw/platform/.done
	@test -n "$(MODEL)" || { echo "usage: make boot MODEL=<name>"; exit 1; }
	$(PY) -m compiler.codegen.export_board --model $(MODEL) --n $(N)
	$(PY) sw/build_boot.py --model $(MODEL)

$(VENV): compiler/requirements.txt
	python3 -m venv .venv
	.venv/bin/pip install -q -r compiler/requirements.txt
	touch $@
