`default_nettype none

// In-order, aligned 64-bit DMA. Command counts of zero are accepted as no-ops.
// AW and W have independent output registers; W may precede its AW handshake.
// From an empty queue: AWVALID after two cycles, earliest AW handshake after three.
module write_dma #(
  parameter int unsigned MAX_OUTSTANDING = 8
) (
  input wire clk,
  input wire rst_ni,

  // AXI master input
  input wire       m_axi_awready,
  input wire       m_axi_wready,
  input wire [0:0] m_axi_bid,
  input wire [1:0] m_axi_bresp,
  input wire       m_axi_bvalid,

  // Command stream input
  input wire        cmd_valid_i,
  input wire [31:0] cmd_addr_i,
  input wire [19:0] cmd_beats_i,

  // Data stream input
  input wire        data_valid_i,
  input wire [63:0] data_i,
  input wire [ 7:0] strb_i,

  // AXI master output
  output wire [ 0:0] m_axi_awid,
  output wire [31:0] m_axi_awaddr,
  output wire [ 7:0] m_axi_awlen,
  output wire [ 2:0] m_axi_awsize,
  output wire [ 1:0] m_axi_awburst,
  output wire        m_axi_awlock,
  output wire [ 3:0] m_axi_awcache,
  output wire [ 2:0] m_axi_awprot,
  output wire [ 3:0] m_axi_awqos,
  output wire [ 3:0] m_axi_awregion,
  output wire        m_axi_awvalid,
  output wire [63:0] m_axi_wdata,
  output wire [ 7:0] m_axi_wstrb,
  output wire        m_axi_wlast,
  output wire        m_axi_wvalid,
  output wire        m_axi_bready,

  // Command stream output
  output wire cmd_ready_o,

  // Data stream output
  output wire data_ready_o,

  // Status output
  output wire  idle_o,
  output logic error_o
);
  localparam int unsigned PTR_W = (MAX_OUTSTANDING > 1) ? $clog2(MAX_OUTSTANDING) : 1;
  localparam int unsigned COUNT_W = $clog2(MAX_OUTSTANDING + 1);

  // Shallow control queues can use distributed RAM; payload is held in the W reg.
  logic [31:0] cmd_addr_q [4];
  logic [19:0] cmd_beats_q[4];
  logic [1:0] cmd_wr_ptr_q, cmd_rd_ptr_q;
  logic [ 2:0] cmd_count_q;
  logic        active_q;
  wire         cmd_push = cmd_valid_i && cmd_ready_o;
  wire         cmd_pop = !active_q && (cmd_count_q != 0);

  logic [31:0] active_addr_q;
  logic [19:0] active_beats_q;
  logic [ 9:0] boundary_beats;
  logic [ 4:0] burst_beats;
  wire         burst_final = active_beats_q == {15'b0, burst_beats};

  logic        aw_valid_q;
  logic [31:0] aw_addr_q;
  logic [ 7:0] aw_len_q;
  wire         aw_fire = aw_valid_q && m_axi_awready;
  wire         b_fire = m_axi_bvalid && m_axi_bready;
  logic [COUNT_W-1:0] outstanding_q, outstanding_d;

  logic [4:0] burst_beats_q[MAX_OUTSTANDING];
  logic [PTR_W-1:0] burst_wr_ptr_q, burst_rd_ptr_q;
  logic [COUNT_W-1:0] burst_count_q;
  logic [4:0] w_index_q;
  logic w_valid_q;
  logic [63:0] w_data_q;
  logic [7:0] w_strb_q;
  logic w_last_q;
  wire data_fire = data_valid_i && data_ready_o;
  wire burst_pop = data_fire && (w_index_q + 5'd1 == burst_beats_q[burst_rd_ptr_q]);
  wire burst_issue = active_q && (!aw_valid_q || m_axi_awready) &&
                     (outstanding_d < COUNT_W'(MAX_OUTSTANDING)) &&
                     ((burst_count_q < COUNT_W'(MAX_OUTSTANDING)) || burst_pop);

  // The only legal response ID is zero; ID does not participate in routing.
  wire unused_bid = ^m_axi_bid;

  assign cmd_ready_o = rst_ni && (cmd_count_q < 3'd4);
  assign data_ready_o = rst_ni && (burst_count_q != 0) && (!w_valid_q || m_axi_wready);
  assign m_axi_awid = 1'b0;
  assign m_axi_awaddr = aw_addr_q;
  assign m_axi_awlen = aw_len_q;
  assign m_axi_awsize = 3'd3;
  assign m_axi_awburst = 2'b01;
  assign m_axi_awlock = 1'b0;
  assign m_axi_awcache = 4'b0011;
  assign m_axi_awprot = 3'b000;
  assign m_axi_awqos = 4'b0000;
  assign m_axi_awregion = 4'b0000;
  assign m_axi_awvalid = aw_valid_q;
  assign m_axi_wdata = w_data_q;
  assign m_axi_wstrb = w_strb_q;
  assign m_axi_wlast = w_last_q;
  assign m_axi_wvalid = w_valid_q;
  assign m_axi_bready = rst_ni;
  assign idle_o = (cmd_count_q == 0) && !active_q && !aw_valid_q &&
                  (burst_count_q == 0) && !w_valid_q && (outstanding_q == 0);

  // Account for handshakes before reserving another registered AW.
  always_comb begin
    boundary_beats = 10'd512 - {1'b0, active_addr_q[11:3]};
    burst_beats    = (active_beats_q >= 20'd16) ? 5'd16 : active_beats_q[4:0];
    if ({5'b0, burst_beats} > boundary_beats) begin
      burst_beats = boundary_beats[4:0];
    end
    outstanding_d = outstanding_q;
    case ({
      aw_fire, b_fire
    })
      2'b10:   outstanding_d = outstanding_q + 1'b1;
      2'b01:   outstanding_d = outstanding_q - 1'b1;
      default: ;
    endcase
  end

  // Queue commands without resetting address/length storage.
  always_ff @(posedge clk) begin
    if (cmd_push) begin
      cmd_addr_q[cmd_wr_ptr_q]  <= cmd_addr_i;
      cmd_beats_q[cmd_wr_ptr_q] <= cmd_beats_i;
    end
    if (!rst_ni) begin
      cmd_wr_ptr_q <= '0;
      cmd_rd_ptr_q <= '0;
      cmd_count_q  <= '0;
    end else begin
      if (cmd_push) cmd_wr_ptr_q <= cmd_wr_ptr_q + 1'b1;
      if (cmd_pop) cmd_rd_ptr_q <= cmd_rd_ptr_q + 1'b1;
      case ({
        cmd_push, cmd_pop
      })
        2'b10:   cmd_count_q <= cmd_count_q + 1'b1;
        2'b01:   cmd_count_q <= cmd_count_q - 1'b1;
        default: ;
      endcase
    end
  end

  // Split commands into AW registers and matching W-length descriptors together.
  always_ff @(posedge clk) begin
    if (cmd_pop) begin
      active_addr_q  <= cmd_addr_q[cmd_rd_ptr_q];
      active_beats_q <= cmd_beats_q[cmd_rd_ptr_q];
    end else if (burst_issue) begin
      active_addr_q  <= active_addr_q + {24'b0, burst_beats, 3'b0};
      active_beats_q <= active_beats_q - {15'b0, burst_beats};
    end
    if (burst_issue) begin
      aw_addr_q                     <= active_addr_q;
      aw_len_q                      <= {3'b0, burst_beats} - 8'd1;
      burst_beats_q[burst_wr_ptr_q] <= burst_beats;
    end
    if (!rst_ni) begin
      active_q       <= 1'b0;
      aw_valid_q     <= 1'b0;
      burst_wr_ptr_q <= '0;
      burst_rd_ptr_q <= '0;
      burst_count_q  <= '0;
    end else begin
      if (cmd_pop) active_q <= cmd_beats_q[cmd_rd_ptr_q] != 0;
      else if (burst_issue && burst_final) active_q <= 1'b0;
      if (!aw_valid_q || m_axi_awready) aw_valid_q <= burst_issue;
      if (burst_issue) begin
        burst_wr_ptr_q <= (burst_wr_ptr_q == PTR_W'(MAX_OUTSTANDING - 1)) ?
                          '0 : burst_wr_ptr_q + 1'b1;
      end
      if (burst_pop) begin
        burst_rd_ptr_q <= (burst_rd_ptr_q == PTR_W'(MAX_OUTSTANDING - 1)) ?
                          '0 : burst_rd_ptr_q + 1'b1;
      end
      case ({
        burst_issue, burst_pop
      })
        2'b10:   burst_count_q <= burst_count_q + 1'b1;
        2'b01:   burst_count_q <= burst_count_q - 1'b1;
        default: ;
      endcase
    end
  end

  // Elastic W output: one cycle input-to-W latency, one beat/cycle throughput.
  always_ff @(posedge clk) begin
    if (data_fire) begin
      w_data_q <= data_i;
      w_strb_q <= strb_i;
      w_last_q <= burst_pop;
    end
    if (!rst_ni) begin
      w_valid_q <= 1'b0;
      w_index_q <= '0;
    end else begin
      if (!w_valid_q || m_axi_wready) w_valid_q <= data_fire;
      if (data_fire) w_index_q <= burst_pop ? '0 : w_index_q + 1'b1;
    end
  end

  // B is always accepted; errors remain set until the next synchronous reset.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      outstanding_q <= '0;
      error_o       <= 1'b0;
    end else begin
      outstanding_q <= outstanding_d;
      if (b_fire && (m_axi_bresp != 2'b00)) error_o <= 1'b1;
    end
  end
endmodule

`default_nettype wire
