`default_nettype none

module accumulator #(
  parameter  int unsigned DEPTH  = 512,
  parameter  int unsigned DATA_W = 256,
  localparam int unsigned ADDR_W = (DEPTH > 1) ? $clog2(DEPTH) : 1
) (
  input wire clk,
  input wire rst_ni,

  // Array result input: one write accepted every cycle.
  input wire              wr_valid_i,
  input wire              wr_bank_i,
  input wire [ADDR_W-1:0] wr_addr_i,
  input wire              wr_accum_i,
  input wire [DATA_W-1:0] wr_data_i,

  // Drain request and response inputs.
  input wire              rd_valid_i,
  input wire              rd_bank_i,
  input wire [ADDR_W-1:0] rd_addr_i,
  input wire              rdata_ready_i,

  // Drain request and response outputs.
  output wire              rd_ready_o,
  output wire              rdata_valid_o,
  output wire [DATA_W-1:0] rdata_o,

  // Write pipeline status.
  output wire busy_o
);
  // A write sampled at edge N reads BRAM at N, registers its sum at N+1,
  // and commits through port A at N+2. Both stages accept one row/cycle.
  // DATA_W is a positive multiple of 32; each lane wraps independently.
  localparam int unsigned LANES = DATA_W / 32;
  localparam int unsigned DRAIN_DEPTH = 4;
  localparam int unsigned DRAIN_PTR_W = $clog2(DRAIN_DEPTH);
  localparam int unsigned DRAIN_COUNT_W = $clog2(DRAIN_DEPTH + 1);

  logic                     wr_read_valid_q;
  logic                     wr_read_bank_q;
  logic [       ADDR_W-1:0] wr_read_addr_q;
  logic                     wr_read_accum_q;
  logic [       DATA_W-1:0] wr_read_data_q;
  logic [       DATA_W-1:0] wr_read_old;
  logic [       DATA_W-1:0] wr_sum;

  logic                     wr_commit_valid_q;
  logic                     wr_commit_bank_q;
  logic [       ADDR_W-1:0] wr_commit_addr_q;
  logic [       DATA_W-1:0] wr_commit_data_q;

  // The previous commit covers a same-edge port A write / port B read.
  // Its value must be forwarded even if BRAM read-during-write is undefined.
  logic                     wr_recent_valid_q;
  logic                     wr_recent_bank_q;
  logic [       ADDR_W-1:0] wr_recent_addr_q;
  logic [       DATA_W-1:0] wr_recent_data_q;

  wire  [       DATA_W-1:0] bank_rdata        [          2];
  logic                     rd_pending_q;
  logic                     rd_bank_q;
  logic [       ADDR_W-1:0] rd_addr_q;
  logic [       DATA_W-1:0] rd_row;
  wire                      rd_accept;
  wire                      rd_pop;

  // Reserved credits include the synchronous BRAM read in flight, so a
  // sudden response stall cannot overrun the FIFO. Ready uses only credits.
  // Request edge N reads BRAM, N+1 enqueues, N+2 can consume the response.
  logic [DRAIN_COUNT_W-1:0] rd_reserved_q;
  logic [  DRAIN_PTR_W-1:0] rd_write_ptr_q;
  logic [  DRAIN_PTR_W-1:0] rd_read_ptr_q;
  logic [       DATA_W-1:0] rd_fifo           [DRAIN_DEPTH];

  assign rd_ready_o    = rst_ni && (rd_reserved_q < DRAIN_COUNT_W'(DRAIN_DEPTH));
  assign rdata_valid_o = rst_ni && (rd_reserved_q > DRAIN_COUNT_W'(rd_pending_q));
  assign rdata_o       = rd_fifo[rd_read_ptr_q];
  assign rd_accept     = rd_valid_i && rd_ready_o;
  assign rd_pop        = rdata_valid_o && rdata_ready_i;
  assign busy_o        = wr_read_valid_q || wr_commit_valid_q;

  // Port B is shared between the current RMW and accepted drain request.
  for (genvar bank = 0; bank < 2; bank++) begin : gen_banks
    wire              bank_write;
    wire              bank_rmw;
    wire              bank_drain;
    wire [ADDR_W-1:0] bank_read_addr;
    wire [DATA_W-1:0] unused_a_rdata;

    assign bank_write     = rst_ni && wr_commit_valid_q && (wr_commit_bank_q == 1'(bank));
    assign bank_rmw       = rst_ni && wr_valid_i && wr_accum_i && (wr_bank_i == 1'(bank));
    assign bank_drain     = rd_accept && (rd_bank_i == 1'(bank));
    assign bank_read_addr = bank_rmw ? wr_addr_i : rd_addr_i;

    ram_2p #(
      .WIDTH (DATA_W),
      .DEPTH (DEPTH),
      .ADDR_W(ADDR_W)
    ) u_ram (
      .clk       (clk),
      .a_en_i    (bank_write),
      .a_we_i    (bank_write),
      .a_addr_i  (wr_commit_addr_q),
      .a_wdata_i (wr_commit_data_q),
      .a_rdata_o (unused_a_rdata),
      .b_en_i    (bank_rmw || bank_drain),
      .b_we_i    (1'b0),
      .b_addr_i  (bank_read_addr),
      .b_wdata_i ({DATA_W{1'b0}}),
      .b_rdata_o (bank_rdata[bank])
    );
  end

  // Most recent matching write wins. Distance one uses the pending commit;
  // distance two uses the previous commit; older writes are already in RAM.
  always_comb begin
    wr_read_old = bank_rdata[wr_read_bank_q];
    if (wr_recent_valid_q && (wr_recent_bank_q == wr_read_bank_q) &&
        (wr_recent_addr_q == wr_read_addr_q)) begin
      wr_read_old = wr_recent_data_q;
    end
    if (wr_commit_valid_q && (wr_commit_bank_q == wr_read_bank_q) &&
        (wr_commit_addr_q == wr_read_addr_q)) begin
      wr_read_old = wr_commit_data_q;
    end

    wr_sum = wr_read_data_q;
    if (wr_read_accum_q) begin
      for (int lane = 0; lane < LANES; lane++) begin
        wr_sum[32*lane+:32] = wr_read_data_q[32*lane+:32] + wr_read_old[32*lane+:32];
      end
    end
  end

  // The same bypasses make a drain immediately following the last write
  // observe that write, including a BRAM port A/B collision at distance two.
  always_comb begin
    rd_row = bank_rdata[rd_bank_q];
    if (wr_recent_valid_q && (wr_recent_bank_q == rd_bank_q) &&
        (wr_recent_addr_q == rd_addr_q)) begin
      rd_row = wr_recent_data_q;
    end
    if (wr_commit_valid_q && (wr_commit_bank_q == rd_bank_q) &&
        (wr_commit_addr_q == rd_addr_q)) begin
      rd_row = wr_commit_data_q;
    end
  end

  // Reset only validity and queue control; memories and datapath retain data.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      wr_read_valid_q   <= 1'b0;
      wr_commit_valid_q <= 1'b0;
      wr_recent_valid_q <= 1'b0;
      rd_pending_q      <= 1'b0;
      rd_reserved_q     <= '0;
      rd_write_ptr_q    <= '0;
      rd_read_ptr_q     <= '0;
    end else begin
      wr_read_valid_q   <= wr_valid_i;
      wr_commit_valid_q <= wr_read_valid_q;
      wr_recent_valid_q <= wr_commit_valid_q;
      rd_pending_q      <= rd_accept;

      case ({
        rd_accept, rd_pop
      })
        2'b10:   rd_reserved_q <= rd_reserved_q + 1'b1;
        2'b01:   rd_reserved_q <= rd_reserved_q - 1'b1;
        default: ;
      endcase
      if (rd_pending_q) rd_write_ptr_q <= rd_write_ptr_q + 1'b1;
      if (rd_pop) rd_read_ptr_q <= rd_read_ptr_q + 1'b1;
    end
  end

  // Data registers use enables but have no reset, allowing BRAM inference.
  always_ff @(posedge clk) begin
    if (wr_valid_i) begin
      wr_read_bank_q  <= wr_bank_i;
      wr_read_addr_q  <= wr_addr_i;
      wr_read_accum_q <= wr_accum_i;
      wr_read_data_q  <= wr_data_i;
    end
    if (wr_read_valid_q) begin
      wr_commit_bank_q <= wr_read_bank_q;
      wr_commit_addr_q <= wr_read_addr_q;
      wr_commit_data_q <= wr_sum;
    end
    if (wr_commit_valid_q) begin
      wr_recent_bank_q <= wr_commit_bank_q;
      wr_recent_addr_q <= wr_commit_addr_q;
      wr_recent_data_q <= wr_commit_data_q;
    end
    if (rd_accept) begin
      rd_bank_q <= rd_bank_i;
      rd_addr_q <= rd_addr_i;
    end
    if (rd_pending_q) rd_fifo[rd_write_ptr_q] <= rd_row;
  end

`ifndef SYNTHESIS
  // Simultaneous accepted requests cannot share a bank's port B, even
  // when the write is an overwrite that would otherwise leave port B idle.
  always_ff @(posedge clk) begin
    if (rst_ni) begin
      assert (!(wr_valid_i && rd_accept && (wr_bank_i == rd_bank_i)))
      else $fatal(1, "accumulator: write and drain requests target the same bank");
    end
  end
`endif
endmodule

`default_nettype wire
