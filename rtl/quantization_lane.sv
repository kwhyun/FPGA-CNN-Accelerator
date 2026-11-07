`default_nettype none
// Seven enabled datapath stages. advance_i freezes every stage together.
// The owner supplies valid tracking and output buffering.
module quantization_lane (
  input wire clk,
  input wire rst_ni,

  // Layer configuration.
  input wire signed [7:0] zp_out_i,
  input wire signed [7:0] act_min_i,
  input wire signed [7:0] act_max_i,

  // Lane pipeline input.
  input wire               advance_i,
  input wire signed [31:0] acc_i,
  input wire signed [31:0] eff_bias_i,
  input wire        [16:0] mult_i,
  input wire        [ 5:0] shift_i,

  // Lane pipeline result.
  output logic signed [7:0] data_o
);
  logic signed [32:0] sum_q;
  logic signed [17:0] mult_q;
  logic signed [49:0] product_q;
  logic signed [49:0] rounded;
  logic signed [50:0] offset_q;
  logic        [ 5:0] shift_q;
  logic        [ 5:0] shift_q2;
  logic signed [ 7:0] zp_q      [5];
  logic signed [ 7:0] min_q     [6];
  logic signed [ 7:0] max_q     [6];

  round_shift u_round_shift (
    .clk       (clk),
    .rst_ni    (rst_ni),
    .advance_i (advance_i),
    .data_i    (product_q),
    .shift_i   (shift_q2),
    .data_o    (rounded)
  );

  // Registered multiplier operands and product permit DSP48E1 inference.
  // M is unsigned 17-bit; its explicit leading zero preserves that meaning.
  always_ff @(posedge clk) begin
    if (rst_ni && advance_i) begin
      sum_q     <= {acc_i[31], acc_i} + {eff_bias_i[31], eff_bias_i};
      mult_q    <= $signed({1'b0, mult_i});
      shift_q   <= shift_i;
      product_q <= sum_q * mult_q;
      shift_q2  <= shift_q;
      zp_q[0]   <= zp_out_i;
      min_q[0]  <= act_min_i;
      max_q[0]  <= act_max_i;
      for (int stage = 1; stage < 5; stage++) begin
        zp_q[stage] <= zp_q[stage-1];
      end
      for (int stage = 1; stage < 6; stage++) begin
        min_q[stage] <= min_q[stage-1];
        max_q[stage] <= max_q[stage-1];
      end
      offset_q <= {rounded[49], rounded} + {{43{zp_q[4][7]}}, zp_q[4]};
      if (offset_q < $signed({{43{min_q[5][7]}}, min_q[5]})) begin
        data_o <= min_q[5];
      end else if (offset_q > $signed({{43{max_q[5][7]}}, max_q[5]})) begin
        data_o <= max_q[5];
      end else begin
        data_o <= offset_q[7:0];
      end
    end
  end
endmodule
`default_nettype wire
