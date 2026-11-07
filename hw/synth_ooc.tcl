# Out-of-context synthesis of one RTL module on XC7Z020-1 at 100 MHz.
#   vivado -mode batch -source hw/synth_ooc.tcl -tclargs <top> <out_dir> [file ...]
# Default sources: all rtl/*.sv.
set top [lindex $argv 0]
set out [lindex $argv 1]
set files [lrange $argv 2 end]
if {[llength $files] == 0} { set files [glob rtl/*.sv] }
file mkdir $out
read_verilog -sv $files
synth_design -top $top -part xc7z020clg484-1 -mode out_of_context -include_dirs rtl
create_clock -period 10.000 -name clk [get_ports clk]
set_input_delay 0 -clock clk [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 0 -clock clk [get_ports -filter {DIRECTION == OUT}]
report_utilization -file $out/utilization.rpt
report_utilization -hierarchical -file $out/hierarchy.rpt
report_timing_summary -max_paths 10 -file $out/timing.rpt
