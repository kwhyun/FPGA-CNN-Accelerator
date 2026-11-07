`default_nettype none
// Fixed 8x8 weight-stationary array, one input/output row per cycle.
// LATENCY = 16: a row sampled at edge E is available after edge E+16.
// Row r has r skew registers; activation forwarding adds c cycles to PE(r,c).
// The MAC adds two cycles after operand capture, and lane c is deskewed by
// DIM-1-c cycles: (DIM-1) + c + 2 + (DIM-1-c) = 2*DIM.
// Bubbles never stall any pipeline. The first row of every tile must swap a
// complete shadow tile in; weights are deliberately not initialized on reset.
// Shadow rows are released one by one: row k is free once the swap marker has
// passed PE(k,DIM-1) (edge E+k+DIM-1), so beat k may load at edge E+k+DIM.
// The next swap is therefore possible at E+2*DIM (one-row tiles are supported).
module ws_array #(
  parameter int unsigned TAG_W = 16
) (
  input wire clk,
  input wire rst_ni,

  // Shadow weight row input
  input wire        w_valid_i,
  input wire [63:0] w_row_i,

  // Activation row input (always accepted)
  input wire             a_valid_i,
  input wire [     63:0] a_row_i,
  input wire             a_swap_i,
  input wire [TAG_W-1:0] a_tag_i,

  // Shadow weight row status
  output wire w_ready_o,
  output wire shadow_full_o,

  // Aligned result row output
  output wire             c_valid_o,
  output wire [    255:0] c_row_o,
  output wire [TAG_W-1:0] c_tag_o
);
  localparam int unsigned DIM = 8;
  localparam int unsigned LATENCY = 2 * DIM;
  localparam int unsigned ROW_W = $clog2(DIM);

  logic       [ROW_W-1:0] weight_row_q;
  logic                   shadow_full_q;
  logic       [  DIM-1:0] row_busy_q;
  wire                    weight_fire;
  wire                    swap_fire;

  wire signed [      7:0] activation_chain        [      DIM] [DIM+1];
  wire                    swap_chain              [      DIM] [DIM+1];
  wire signed [     31:0] psum_chain              [    DIM+1] [  DIM];
  wire        [8*DIM-1:0] unused_activation_right;
  wire        [  DIM-1:0] unused_swap_right;
  logic       [LATENCY:0] valid_q;
  logic       [TAG_W-1:0] tag_q                   [LATENCY+1];

  assign w_ready_o     = rst_ni && !shadow_full_q && !row_busy_q[weight_row_q];
  assign shadow_full_o = shadow_full_q;
  assign weight_fire   = w_valid_i && w_ready_o;
  assign swap_fire     = a_valid_i && a_swap_i;

  // A shadow row becomes reusable when the last PE of that row captures the swap
  // row; its old weight has then been copied into operands for every older row.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      weight_row_q  <= '0;
      shadow_full_q <= 1'b0;
      row_busy_q    <= '0;
    end else begin
      if (weight_fire) begin
        if (weight_row_q == ROW_W'(DIM - 1)) begin
          weight_row_q  <= '0;
          shadow_full_q <= 1'b1;
        end else begin
          weight_row_q <= weight_row_q + 1'b1;
        end
      end
      if (swap_fire) shadow_full_q <= 1'b0;
      for (int row = 0; row < DIM; row++) begin
        if (swap_fire) row_busy_q[row] <= 1'b1;
        else if (swap_chain[row][DIM-1]) row_busy_q[row] <= 1'b0;
      end
    end
  end

  // Metadata advances on every clock, including bubbles.
  always_ff @(posedge clk) begin
    if (!rst_ni) valid_q <= '0;
    else valid_q <= {valid_q[LATENCY-1:0], a_valid_i};
  end
  always_ff @(posedge clk) begin
    tag_q[0] <= a_tag_i;
    for (int stage = 1; stage <= LATENCY; stage++) begin
      tag_q[stage] <= tag_q[stage-1];
    end
  end
  assign c_valid_o = valid_q[LATENCY];
  assign c_tag_o   = tag_q[LATENCY];

  for (genvar row = 0; row < DIM; row++) begin : gen_rows
    wire weight_load;
    assign weight_load = weight_fire && (weight_row_q == ROW_W'(row));

    // Delay lane k=r and its swap marker by exactly r cycles.
    if (row == 0) begin : gen_no_skew
      assign activation_chain[row][0] = $signed(a_row_i[8*row+:8]);
      assign swap_chain[row][0]       = swap_fire;
    end else begin : gen_skew
      logic signed [    7:0] activation_q[row];
      logic        [row-1:0] swap_q;
      always_ff @(posedge clk) begin
        activation_q[0] <= $signed(a_row_i[8*row+:8]);
        for (int stage = 1; stage < row; stage++) begin
          activation_q[stage] <= activation_q[stage-1];
        end
      end
      always_ff @(posedge clk) begin
        if (!rst_ni) swap_q <= '0;
        else swap_q <= (swap_q << 1) | row'(swap_fire);
      end
      assign activation_chain[row][0] = activation_q[row-1];
      assign swap_chain[row][0]       = swap_q[row-1];
    end

    for (genvar col = 0; col < DIM; col++) begin : gen_cols
      ws_pe u_pe (
        .clk      (clk),
        .rst_ni   (rst_ni),

        // Activation and tile boundary input
        .a_i      (activation_chain[row][col]),
        .a_swap_i (swap_chain[row][col]),

        // Shadow weight load and upstream partial sum
        .w_load_i (weight_load),
        .w_i      ($signed(w_row_i[8*col+:8])),
        .psum_i   (psum_chain[row][col]),

        // Activation and tile boundary forwarding
        .a_o      (activation_chain[row][col+1]),
        .a_swap_o (swap_chain[row][col+1]),

        // Downstream partial sum
        .psum_o   (psum_chain[row+1][col])
      );
    end
    // The right edge has no consumer; only the lower partial sums leave.
    assign unused_activation_right[8*row+:8] = activation_chain[row][DIM];
    assign unused_swap_right[row]            = swap_chain[row][DIM];
  end

  for (genvar col = 0; col < DIM; col++) begin : gen_deskew
    assign psum_chain[0][col] = '0;
    if (col == DIM - 1) begin : gen_no_delay
      assign c_row_o[32*col+:32] = psum_chain[DIM][col];
    end else begin : gen_delay
      localparam int unsigned DELAY = DIM - 1 - col;
      logic signed [31:0] result_q[DELAY];
      always_ff @(posedge clk) begin
        result_q[0] <= psum_chain[DIM][col];
        for (int stage = 1; stage < DELAY; stage++) begin
          result_q[stage] <= result_q[stage-1];
        end
      end
      assign c_row_o[32*col+:32] = result_q[DELAY-1];
    end
  end
endmodule
`default_nettype wire
