# PS7(ZedBoard) + accel_top system: block design, synthesis, implementation, bitstream, XSA.
#   vivado -mode batch -nojournal -nolog -source hw/system_impl.tcl
# FCLK0 = 100 MHz from PS7. Outputs: build/hw/ (accel_hw.xsa, *.rpt)
set root [file normalize [file join [file dirname [info script]] ..]]
set work $root/build/hw/vivado
set out  $root/build/hw
file mkdir $out
set_param board.repoPaths [list [file normalize ~/.Xilinx/Vivado/2025.2/xhub/board_store/xilinx_board_store]]
if {[llength [get_board_parts -quiet avnet.com:zedboard:part0:1.4]] == 0} {
  xhub::refresh_catalog [xhub::get_xstores xilinx_board_store]
  xhub::install [xhub::get_xitems avnet.com:xilinx_board_store:zedboard:1.4]
  set_param board.repoPaths [get_param board.repoPaths]
}

create_project -force accel_system $work -part xc7z020clg484-1
set_property board_part avnet.com:zedboard:part0:1.4 [current_project]
set_property target_language Verilog [current_project]
set_property include_dirs [list $root/rtl] [get_filesets sources_1]
add_files [glob $root/rtl/*.sv]
add_files $root/rtl/accel_wrap.v
add_files $root/rtl/accel_regs.svh
set_property file_type {Verilog Header} [get_files accel_regs.svh]
update_compile_order -fileset sources_1

create_bd_design accel_bd
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7]
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 -config {make_external "FIXED_IO, DDR" apply_board_preset "1"} $ps
set_property -dict [list \
  CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
  CONFIG.PCW_USE_M_AXI_GP0 {1} \
  CONFIG.PCW_USE_S_AXI_HP0 {1} CONFIG.PCW_S_AXI_HP0_DATA_WIDTH {64} \
  CONFIG.PCW_USE_S_AXI_HP1 {1} CONFIG.PCW_S_AXI_HP1_DATA_WIDTH {64} \
  CONFIG.PCW_USE_S_AXI_HP2 {1} CONFIG.PCW_S_AXI_HP2_DATA_WIDTH {64} \
  CONFIG.PCW_USE_FABRIC_INTERRUPT {1} CONFIG.PCW_IRQ_F2P_INTR {1} \
] $ps
set acc [create_bd_cell -type module -reference accel_wrap accel]
set rst [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_100m]
set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axil_smc]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $smc

set clk [get_bd_pins ps7/FCLK_CLK0]
foreach p {ps7/M_AXI_GP0_ACLK ps7/S_AXI_HP0_ACLK ps7/S_AXI_HP1_ACLK ps7/S_AXI_HP2_ACLK accel/clk rst_100m/slowest_sync_clk axil_smc/aclk} {
  connect_bd_net $clk [get_bd_pins $p]
}
connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins rst_100m/ext_reset_in]
connect_bd_net [get_bd_pins rst_100m/peripheral_aresetn] [get_bd_pins accel/rst_ni]
connect_bd_net [get_bd_pins rst_100m/interconnect_aresetn] [get_bd_pins axil_smc/aresetn]
connect_bd_intf_net [get_bd_intf_pins ps7/M_AXI_GP0] [get_bd_intf_pins axil_smc/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axil_smc/M00_AXI] [get_bd_intf_pins accel/s_axil]
# PS7 HP ports are AXI3: one protocol converter per AXI4 master (bursts ≤ 16, so no splitting)
foreach {m hp} {m_axi_in S_AXI_HP0 m_axi_w S_AXI_HP1 m_axi_out S_AXI_HP2} {
  set pc [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter:2.1 pc_$m]
  set_property -dict [list CONFIG.MI_PROTOCOL {AXI3}] $pc
  connect_bd_net $clk [get_bd_pins pc_$m/aclk]
  connect_bd_net [get_bd_pins rst_100m/interconnect_aresetn] [get_bd_pins pc_$m/aresetn]
  connect_bd_intf_net [get_bd_intf_pins accel/$m] [get_bd_intf_pins pc_$m/S_AXI]
  connect_bd_intf_net [get_bd_intf_pins pc_$m/M_AXI] [get_bd_intf_pins ps7/$hp]
}
connect_bd_net [get_bd_pins accel/irq_o] [get_bd_pins ps7/IRQ_F2P]
assign_bd_address
validate_bd_design
save_bd_design
write_bd_tcl -force $out/recreate_bd.tcl

set_property synth_checkpoint_mode None [get_files accel_bd.bd]
generate_target all [get_files accel_bd.bd]
make_wrapper -files [get_files accel_bd.bd] -top -import
set_property top accel_bd_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

launch_runs synth_1 -jobs 8
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "synthesis failed"}
launch_runs impl_1 -to_step route_design -jobs 8
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {error "route failed"}
open_run impl_1
report_clocks -file $out/clocks.rpt
report_timing_summary -max_paths 20 -file $out/timing.rpt
report_utilization -file $out/utilization.rpt
report_utilization -hierarchical -file $out/hierarchy.rpt
report_drc -file $out/drc.rpt
set period [get_property PERIOD [get_clocks clk_fpga_0]]
if {abs($period - 10.0) > 1e-6} {error "unexpected FCLK period $period"}
set setup [get_property SLACK [get_timing_paths -delay_type max -max_paths 1]]
set hold  [get_property SLACK [get_timing_paths -delay_type min -max_paths 1]]
puts "TIMING setup_wns=$setup hold_whs=$hold"
if {$setup < 0 || $hold < 0} {error "timing not met; no bitstream"}
close_design
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
open_run impl_1
write_hw_platform -fixed -include_bit -force -file $out/accel_hw.xsa
puts "SYSTEM_DONE"
