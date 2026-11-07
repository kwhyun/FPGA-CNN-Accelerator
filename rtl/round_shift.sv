`default_nettype none
// Three enabled stages: magnitude, variable shift, signed rounding.
// S is in [1,63]. Shifting by S-1 then rounding bit zero avoids a
// wide variable half-value adder and handles S greater than WIDTH exactly.
module round_shift #(
  parameter int unsigned WIDTH = 50
) (
  input wire clk,
  input wire rst_ni,

  // Pipeline input (all stages advance together).
  input wire                    advance_i,
  input wire signed [WIDTH-1:0] data_i,
  input wire        [      5:0] shift_i,

  // Pipeline result.
  output logic signed [WIDTH-1:0] data_o
);
  logic [WIDTH-1:0] magnitude_q;
  logic [WIDTH-1:0] shifted_q;
  logic [WIDTH-1:0] rounded_magnitude;
  logic [      5:0] shift_q;
  logic             negative_q;
  logic             negative_q2;

  assign rounded_magnitude = (shifted_q >> 1) + {{(WIDTH - 1) {1'b0}}, shifted_q[0]};

  // Data registers need no reset; the owner resets the associated valid bits.
  always_ff @(posedge clk) begin
    if (rst_ni && advance_i) begin
      magnitude_q <= data_i[WIDTH-1] ? $unsigned(-data_i) : $unsigned(data_i);
      negative_q  <= data_i[WIDTH-1];
      shift_q     <= shift_i;
      shifted_q   <= magnitude_q >> (shift_q - 6'd1);
      negative_q2 <= negative_q;
      data_o      <= negative_q2 ? -$signed(rounded_magnitude) : $signed(rounded_magnitude);
    end
  end
endmodule
`default_nettype wire
