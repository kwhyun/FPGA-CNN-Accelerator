`default_nettype none
// One INT8 MAC: registered operands, registered product, registered INT32 sum.
// Activation/swap forwarding takes one cycle. The partial sum is sampled at
// the sum stage, two edges after the corresponding activation is sampled.
// DSP48E1: A*B -> MREG -> P = C(psum_i) + M -> PREG.
// use_dsp forces the DSP; Vivado otherwise maps the 8x8 multiply to LUTs.
(* use_dsp = "yes" *)
module ws_pe (
  input wire clk,
  input wire rst_ni,

  // Activation and tile boundary input
  input wire signed [7:0] a_i,
  input wire              a_swap_i,

  // Shadow weight load and upstream registered partial sum
  input wire               w_load_i,
  input wire signed [ 7:0] w_i,
  input wire signed [31:0] psum_i,

  // Activation and tile boundary forwarding
  output wire signed [7:0] a_o,
  output wire              a_swap_o,

  // Downstream registered partial sum
  output logic signed [31:0] psum_o
);
  logic signed [ 7:0] weight0_q;
  logic signed [ 7:0] weight1_q;
  logic               active_bank_q;
  logic               swap_q;
  logic signed [ 7:0] activation_q;
  logic signed [ 7:0] weight_q;
  logic signed [15:0] product_q;

  assign a_o      = activation_q;
  assign a_swap_o = swap_q;

  // Only ownership/marker state needs reset; weights and arithmetic do not.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      active_bank_q <= 1'b0;
      swap_q        <= 1'b0;
    end else begin
      swap_q <= a_swap_i;
      if (a_swap_i) active_bank_q <= ~active_bank_q;
    end
  end

  // The swap row captures the new bank immediately. Earlier rows already
  // captured their weight, so retiring that bank cannot change their product.
  always_ff @(posedge clk) begin
    if (w_load_i) begin
      if (active_bank_q) weight0_q <= w_i;
      else weight1_q <= w_i;
    end
    activation_q <= a_i;
    weight_q     <= (active_bank_q ^ a_swap_i) ? weight1_q : weight0_q;
    product_q    <= activation_q * weight_q;
    psum_o       <= psum_i + $signed({{16{product_q[15]}}, product_q});
  end
endmodule
`default_nettype wire
