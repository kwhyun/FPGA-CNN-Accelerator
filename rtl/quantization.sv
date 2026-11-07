`default_nettype none
// Eight cycles from input transfer to earliest output transfer, without stalls:
// seven lane stages plus the output skid register. Sustained rate: one row/clock.
module quantization (
  input wire clk,
  input wire rst_ni,

  // Layer configuration.
  input wire [7:0] zp_out_i,
  input wire [7:0] act_min_i,
  input wire [7:0] act_max_i,

  // Input stream and lane coefficients.
  input wire         in_valid_i,
  input wire [255:0] in_acc_i,
  // Reserved coefficient bits are intentionally unused.
  /* verilator lint_off UNUSEDSIGNAL */
  input wire [511:0] in_coef_i,
  /* verilator lint_on UNUSEDSIGNAL */

  // Output stream consumer.
  input wire out_ready_i,

  // Input stream flow control.
  output wire in_ready_o,

  // Output stream.
  output wire        out_valid_o,
  output wire [63:0] out_data_o
);
  localparam int unsigned LATENCY = 8;
  logic [LATENCY-2:0] valid_q;
  wire                advance;
  wire  [       63:0] lane_data;
  logic [        1:0] skid_count_q;
  logic [       63:0] skid_head_q;
  logic [       63:0] skid_tail_q;
  wire                skid_push;
  wire                skid_pop;

  // Registered occupancy breaks the downstream-ready path. The second slot
  // catches a result on the cycle that the consumer withdraws ready.
  assign advance     = skid_count_q != 2'd2;
  assign in_ready_o  = rst_ni && advance;
  assign out_valid_o = rst_ni && (skid_count_q != 2'd0);
  assign out_data_o  = skid_head_q;
  assign skid_push   = advance && valid_q[LATENCY-2];
  assign skid_pop    = out_valid_o && out_ready_i;

  for (genvar lane = 0; lane < 8; lane++) begin : gen_lane
    // Reserved mult_word bits are intentionally ignored by the contract.
    quantization_lane u_quantization_lane (
      .clk        (clk),
      .rst_ni     (rst_ni),
      .zp_out_i   (zp_out_i),
      .act_min_i  (act_min_i),
      .act_max_i  (act_max_i),
      .advance_i  (advance),
      .acc_i      (in_acc_i[32*lane+:32]),
      .eff_bias_i (in_coef_i[64*lane+:32]),
      .mult_i     (in_coef_i[64*lane+32+:17]),
      .shift_i    (in_coef_i[64*lane+56+:6]),
      .data_o     (lane_data[8*lane+:8])
    );
  end

  // Valid state alone is reset; no arithmetic or skid data registers are reset.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      valid_q      <= '0;
      skid_count_q <= '0;
    end else begin
      if (advance) valid_q <= {valid_q[LATENCY-3:0], in_valid_i};
      case ({
        skid_push, skid_pop
      })
        2'b10:   skid_count_q <= skid_count_q + 2'd1;
        2'b01:   skid_count_q <= skid_count_q - 2'd1;
        default: ;
      endcase
    end
  end

  // Two flip-flop slots, with a fall-through replacement on simultaneous pop/push.
  always_ff @(posedge clk) begin
    if (rst_ni) begin
      if (skid_pop && (skid_count_q == 2'd2)) begin
        skid_head_q <= skid_tail_q;
      end else if (skid_push && ((skid_count_q == 2'd0) || skid_pop)) begin
        skid_head_q <= lane_data;
      end
      if (skid_push && !skid_pop && (skid_count_q == 2'd1)) begin
        skid_tail_q <= lane_data;
      end
    end
  end
endmodule
`default_nettype wire
