`default_nettype none
// AXI-Lite accepts independent AW/W channels and holds B/R under backpressure.
module control_status (
  input  wire         clk,
  input  wire         rst_ni,
  input  wire  [11:0] s_axil_awaddr,
  input  wire  [ 2:0] s_axil_awprot,
  input  wire         s_axil_awvalid,
  input  wire  [31:0] s_axil_wdata,
  input  wire  [ 3:0] s_axil_wstrb,
  input  wire         s_axil_wvalid,
  input  wire         s_axil_bready,
  input  wire  [11:0] s_axil_araddr,
  input  wire  [ 2:0] s_axil_arprot,
  input  wire         s_axil_arvalid,
  input  wire         s_axil_rready,
  input  wire         busy_i,
  input  wire         done_i,
  input  wire         error_i,
  input  wire  [31:0] layer_index_i,
  input  wire  [31:0] error_code_i,
  input  wire  [ 6:0] perf_events_i,
  output wire         s_axil_awready,
  output wire         s_axil_wready,
  output wire  [ 1:0] s_axil_bresp,
  output logic        s_axil_bvalid,
  output wire         s_axil_arready,
  output logic [31:0] s_axil_rdata,
  output wire  [ 1:0] s_axil_rresp,
  output logic        s_axil_rvalid,
  output logic        start_o,
  output logic        soft_reset_o,
  output logic [31:0] instr_base_o,
  output logic [31:0] instr_count_o,
  output wire         irq_o
);
  // Generated register definitions also describe fields unused by this slave.
  /* verilator lint_off UNUSEDPARAM */
  `include "accel_regs.svh"
  /* verilator lint_on UNUSEDPARAM */
  logic aw_full_q, w_full_q;
  logic [11:0] awaddr_q;
  logic [31:0] wdata_q;
  logic [ 3:0] wstrb_q;
  logic done_q, error_q;
  logic [1:0] irq_enable_q, irq_status_q;
  logic [31:0] perf_select_q;
  logic [31:0] perf_q[7];
  wire write_fire = aw_full_q && w_full_q && !s_axil_bvalid;
  wire [31:0] byte_mask = {{8{wstrb_q[3]}}, {8{wstrb_q[2]}}, {8{wstrb_q[1]}}, {8{wstrb_q[0]}}};
  wire unused_protection = ^{s_axil_awprot, s_axil_arprot, s_axil_awaddr[1:0], s_axil_araddr[1:0]};
  assign s_axil_awready = !aw_full_q;
  assign s_axil_wready  = !w_full_q;
  assign s_axil_bresp   = 2'b00;
  assign s_axil_arready = !s_axil_rvalid || s_axil_rready;
  assign s_axil_rresp   = 2'b00;
  assign irq_o          = |(irq_status_q & irq_enable_q);
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      aw_full_q     <= 0;
      w_full_q      <= 0;
      s_axil_bvalid <= 0;
      s_axil_rvalid <= 0;
      start_o       <= 0;
      soft_reset_o  <= 0;
      instr_base_o  <= 0;
      instr_count_o <= 0;
      irq_enable_q  <= 0;
      irq_status_q  <= 0;
      perf_select_q <= 0;
      done_q        <= 0;
      error_q       <= 0;
      for (int counter = 0; counter < 7; counter++) perf_q[counter] <= 0;
    end else begin
      start_o      <= 0;
      soft_reset_o <= 0;
      if (s_axil_awvalid && s_axil_awready) begin
        awaddr_q  <= {s_axil_awaddr[11:2], 2'b0};
        aw_full_q <= 1;
      end
      if (s_axil_wvalid && s_axil_wready) begin
        wdata_q  <= s_axil_wdata;
        wstrb_q  <= s_axil_wstrb;
        w_full_q <= 1;
      end
      if (s_axil_bvalid && s_axil_bready) s_axil_bvalid <= 0;
      if (write_fire) begin
        aw_full_q     <= 0;
        w_full_q      <= 0;
        s_axil_bvalid <= 1;
        case (awaddr_q)
          CTRL_OFFSET:
          if (wstrb_q[0]) begin
            if (wdata_q[CTRL_START_BIT] && !busy_i) begin
              start_o <= 1;
              done_q  <= 0;
              error_q <= 0;
            end
            soft_reset_o <= wdata_q[CTRL_SOFT_RESET_BIT];
          end
          INSTR_BASE_OFFSET: instr_base_o <= (instr_base_o & ~byte_mask) | (wdata_q & byte_mask);
          INSTR_COUNT_OFFSET: instr_count_o <= (instr_count_o & ~byte_mask) | (wdata_q & byte_mask);
          IRQ_EN_OFFSET: if (wstrb_q[0]) irq_enable_q <= wdata_q[1:0];
          IRQ_STATUS_OFFSET: if (wstrb_q[0]) irq_status_q <= irq_status_q & ~wdata_q[1:0];
          PERF_SELECT_OFFSET: perf_select_q <= (perf_select_q & ~byte_mask) | (wdata_q & byte_mask);
          default: ;
        endcase
      end
      if (done_i) begin
        done_q          <= 1;
        irq_status_q[0] <= 1;
      end
      if (error_i) begin
        error_q         <= 1;
        irq_status_q[1] <= 1;
      end
      for (int counter = 0; counter < 7; counter++) begin
        if (start_o) perf_q[counter] <= 0;
        else if (perf_events_i[counter]) perf_q[counter] <= perf_q[counter] + 1'b1;
      end
      if (s_axil_rvalid && s_axil_rready) s_axil_rvalid <= 0;
      if (s_axil_arvalid && s_axil_arready) begin
        s_axil_rvalid <= 1;
        case ({
          s_axil_araddr[11:2], 2'b0
        })
          CTRL_OFFSET: s_axil_rdata <= 0;
          STATUS_OFFSET: s_axil_rdata <= {29'b0, error_q, done_q, busy_i};
          IRQ_EN_OFFSET: s_axil_rdata <= {30'b0, irq_enable_q};
          IRQ_STATUS_OFFSET: s_axil_rdata <= {30'b0, irq_status_q};
          INSTR_BASE_OFFSET: s_axil_rdata <= instr_base_o;
          INSTR_COUNT_OFFSET: s_axil_rdata <= instr_count_o;
          LAYER_INDEX_OFFSET: s_axil_rdata <= layer_index_i;
          ERROR_CODE_OFFSET: s_axil_rdata <= error_code_i;
          PERF_SELECT_OFFSET: s_axil_rdata <= perf_select_q;
          PERF_DATA_OFFSET: s_axil_rdata <= perf_select_q < 7 ? perf_q[perf_select_q[2:0]] : 0;
          HW_VERSION_OFFSET: s_axil_rdata <= 32'h0001_0000;
          default: s_axil_rdata <= 0;
        endcase
      end
    end
  end
endmodule
`default_nettype wire
