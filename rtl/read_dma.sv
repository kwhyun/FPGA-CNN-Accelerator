`default_nettype none

// In-order, aligned 64-bit DMA. Command counts of zero are accepted as no-ops.
// AR is registered; R is a transparent stream (one beat/cycle, no extra latency).
// From an empty queue: ARVALID after two cycles, earliest AR handshake after three.
module read_dma #(
  parameter int unsigned MAX_OUTSTANDING = 8
) (
  input wire clk,
  input wire rst_ni,

  // AXI master input
  input wire        m_axi_arready,
  input wire [ 0:0] m_axi_rid,
  input wire [63:0] m_axi_rdata,
  input wire [ 1:0] m_axi_rresp,
  input wire        m_axi_rlast,
  input wire        m_axi_rvalid,

  // Command stream input
  input wire        cmd_valid_i,
  input wire [31:0] cmd_addr_i,
  input wire [19:0] cmd_beats_i,

  // Data stream input
  input wire data_ready_i,

  // AXI master output
  output wire [ 0:0] m_axi_arid,
  output wire [31:0] m_axi_araddr,
  output wire [ 7:0] m_axi_arlen,
  output wire [ 2:0] m_axi_arsize,
  output wire [ 1:0] m_axi_arburst,
  output wire        m_axi_arlock,
  output wire [ 3:0] m_axi_arcache,
  output wire [ 2:0] m_axi_arprot,
  output wire [ 3:0] m_axi_arqos,
  output wire [ 3:0] m_axi_arregion,
  output wire        m_axi_arvalid,
  output wire        m_axi_rready,

  // Command stream output
  output wire cmd_ready_o,

  // Data stream output
  output wire        data_valid_o,
  output wire [63:0] data_o,
  output wire        data_last_o,

  // Status output
  output logic error_o
);
  localparam int unsigned PTR_W = (MAX_OUTSTANDING > 1) ? $clog2(MAX_OUTSTANDING) : 1;
  localparam int unsigned COUNT_W = $clog2(MAX_OUTSTANDING + 1);

  // Shallow control FIFOs can use distributed RAM; no payload memory is needed.
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

  logic        ar_valid_q;
  logic [31:0] ar_addr_q;
  logic [ 7:0] ar_len_q;
  logic        ar_final_q;
  wire         ar_fire = ar_valid_q && m_axi_arready;
  wire         r_fire = m_axi_rvalid && m_axi_rready;
  wire         burst_retire = r_fire && m_axi_rlast;

  logic        burst_final_q                                        [MAX_OUTSTANDING];
  logic [PTR_W-1:0] burst_wr_ptr_q, burst_rd_ptr_q;
  logic [COUNT_W-1:0] outstanding_q, outstanding_d;
  wire burst_issue = active_q && (!ar_valid_q || m_axi_arready) &&
                     (outstanding_d < COUNT_W'(MAX_OUTSTANDING));

  // The only legal response ID is zero; ID does not participate in routing.
  wire unused_rid = ^m_axi_rid;

  assign cmd_ready_o    = rst_ni && (cmd_count_q < 3'd4);
  assign m_axi_arid     = 1'b0;
  assign m_axi_araddr   = ar_addr_q;
  assign m_axi_arlen    = ar_len_q;
  assign m_axi_arsize   = 3'd3;
  assign m_axi_arburst  = 2'b01;
  assign m_axi_arlock   = 1'b0;
  assign m_axi_arcache  = 4'b0011;
  assign m_axi_arprot   = 3'b000;
  assign m_axi_arqos    = 4'b0000;
  assign m_axi_arregion = 4'b0000;
  assign m_axi_arvalid  = ar_valid_q;
  assign m_axi_rready   = data_ready_i;
  assign data_valid_o   = m_axi_rvalid && rst_ni;
  assign data_o         = m_axi_rdata;
  assign data_last_o    = m_axi_rlast && burst_final_q[burst_rd_ptr_q];

  // Bound each burst by both the remaining command and the next 4 KiB boundary.
  always_comb begin
    boundary_beats = 10'd512 - {1'b0, active_addr_q[11:3]};
    burst_beats    = (active_beats_q >= 20'd16) ? 5'd16 : active_beats_q[4:0];
    if ({5'b0, burst_beats} > boundary_beats) begin
      burst_beats = boundary_beats[4:0];
    end
    outstanding_d = outstanding_q;
    case ({
      ar_fire, burst_retire
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

  // An AR register can be replaced on its handshake, sustaining one AR/cycle.
  always_ff @(posedge clk) begin
    if (cmd_pop) begin
      active_addr_q  <= cmd_addr_q[cmd_rd_ptr_q];
      active_beats_q <= cmd_beats_q[cmd_rd_ptr_q];
    end else if (burst_issue) begin
      active_addr_q  <= active_addr_q + {24'b0, burst_beats, 3'b0};
      active_beats_q <= active_beats_q - {15'b0, burst_beats};
    end
    if (burst_issue) begin
      ar_addr_q  <= active_addr_q;
      ar_len_q   <= {3'b0, burst_beats} - 8'd1;
      ar_final_q <= burst_final;
    end
    if (!rst_ni) begin
      active_q   <= 1'b0;
      ar_valid_q <= 1'b0;
    end else begin
      if (cmd_pop) active_q <= cmd_beats_q[cmd_rd_ptr_q] != 0;
      else if (burst_issue && burst_final) active_q <= 1'b0;
      if (!ar_valid_q || m_axi_arready) ar_valid_q <= burst_issue;
    end
  end

  // One marker per accepted AR reconstructs command LAST across split bursts.
  always_ff @(posedge clk) begin
    if (ar_fire) burst_final_q[burst_wr_ptr_q] <= ar_final_q;
    if (!rst_ni) begin
      burst_wr_ptr_q <= '0;
      burst_rd_ptr_q <= '0;
      outstanding_q  <= '0;
      error_o        <= 1'b0;
    end else begin
      outstanding_q <= outstanding_d;
      if (ar_fire) begin
        burst_wr_ptr_q <= (burst_wr_ptr_q == PTR_W'(MAX_OUTSTANDING - 1)) ?
                          '0 : burst_wr_ptr_q + 1'b1;
      end
      if (burst_retire) begin
        burst_rd_ptr_q <= (burst_rd_ptr_q == PTR_W'(MAX_OUTSTANDING - 1)) ?
                          '0 : burst_rd_ptr_q + 1'b1;
      end
      if (r_fire && (m_axi_rresp != 2'b00)) error_o <= 1'b1;
    end
  end
endmodule

`default_nettype wire
