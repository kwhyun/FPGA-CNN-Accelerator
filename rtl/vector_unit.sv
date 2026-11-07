`default_nettype none
// Nine cycles from a result-producing input transfer to its earliest output
// transfer without stalls. ADD produces every row; MAX/AVG produce on last.
// Two 18x18 signed multipliers per lane serve both ADD and split-word AVG.
module vector_unit (
  input wire clk,
  input wire rst_ni,

  // Operation and layer configuration (fixed for the operation).
  input wire        [ 1:0] op_i,
  input wire signed [ 7:0] zp_a_i,
  input wire signed [ 7:0] zp_b_i,
  input wire signed [ 7:0] zp_out_i,
  input wire signed [ 7:0] act_min_i,
  input wire signed [ 7:0] act_max_i,
  // Reserved coefficient bits are intentionally unused.
  /* verilator lint_off UNUSEDSIGNAL */
  input wire        [31:0] scalar0_i,
  /* verilator lint_on UNUSEDSIGNAL */
  input wire        [31:0] scalar1_i,

  // Input stream; first and last may both be set for a singleton group.
  input wire        in_valid_i,
  input wire [63:0] in_a_i,
  input wire [63:0] in_b_i,
  input wire        in_first_i,
  input wire        in_last_i,

  // Output stream consumer.
  input wire out_ready_i,

  // Input stream flow control.
  output wire in_ready_o,

  // Output stream.
  output wire        out_valid_o,
  output wire [63:0] out_data_o
);
  localparam logic [1:0] OP_ADD = 2'd0;
  localparam logic [1:0] OP_MAX = 2'd1;
  localparam logic [1:0] OP_AVG = 2'd2;
  localparam int unsigned LATENCY = 9;

  logic        [LATENCY-2:0] valid_q;
  wire                       advance;
  logic                      avg_q;
  logic                      avg_q2;
  logic        [        5:0] shift_q      [3];
  logic signed [        7:0] zp_q         [6];
  logic signed [        7:0] min_q        [7];
  logic signed [        7:0] max_q        [7];
  wire         [       63:0] lane_data;
  logic        [        1:0] skid_count_q;
  logic        [       63:0] skid_head_q;
  logic        [       63:0] skid_tail_q;
  wire                       skid_push;
  wire                       skid_pop;

  // Both the pipeline and group accumulators stop when the skid buffer is full.
  assign advance     = skid_count_q != 2'd2;
  assign in_ready_o  = rst_ni && advance;
  assign out_valid_o = rst_ni && (skid_count_q != 2'd0);
  assign out_data_o  = skid_head_q;
  assign skid_push   = advance && valid_q[LATENCY-2];
  assign skid_pop    = out_valid_o && out_ready_i;

  // Configuration follows each result through the arithmetic stages.
  always_ff @(posedge clk) begin
    if (rst_ni && advance) begin
      avg_q      <= op_i == OP_AVG;
      avg_q2     <= avg_q;
      shift_q[0] <= (op_i == OP_MAX) ? 6'd1 : scalar0_i[29:24];
      shift_q[1] <= shift_q[0];
      shift_q[2] <= shift_q[1];
      zp_q[0]    <= (op_i == OP_MAX) ? 8'sd0 : zp_out_i;
      min_q[0]   <= act_min_i;
      max_q[0]   <= act_max_i;
      for (int stage = 1; stage < 6; stage++) begin
        zp_q[stage] <= zp_q[stage-1];
      end
      for (int stage = 1; stage < 7; stage++) begin
        min_q[stage] <= min_q[stage-1];
        max_q[stage] <= max_q[stage-1];
      end
    end
  end

  for (genvar lane = 0; lane < 8; lane++) begin : gen_lane
    wire signed  [ 7:0] input_a;
    wire signed  [ 7:0] input_b;
    wire signed  [ 8:0] centered_a;
    wire signed  [ 8:0] centered_b;
    logic signed [31:0] sum_q;
    wire signed  [31:0] sum_next;
    wire signed  [32:0] biased_sum;
    logic signed [ 7:0] maximum_q;
    wire signed  [ 7:0] maximum_next;
    logic signed [17:0] operand_a_q;
    logic signed [17:0] operand_b_q;
    logic signed [17:0] mult_a_q;
    logic signed [17:0] mult_b_q;
    logic signed [35:0] product_a_q;
    logic signed [35:0] product_b_q;
    logic signed [49:0] combined_q;
    wire signed  [49:0] product_a_extended;
    wire signed  [49:0] product_b_extended;
    wire signed  [49:0] rounded;
    logic signed [50:0] offset_q;
    logic signed [ 7:0] clamped_q;

    assign input_a = $signed(in_a_i[8*lane+:8]);
    assign input_b = $signed(in_b_i[8*lane+:8]);
    assign centered_a = {input_a[7], input_a} - {zp_a_i[7], zp_a_i};
    assign centered_b = {input_b[7], input_b} - {zp_b_i[7], zp_b_i};
    // The compiler guarantees the INT32 reduction sum does not overflow.
    assign sum_next = in_first_i ? {{24{input_a[7]}}, input_a} :
        sum_q + {{24{input_a[7]}}, input_a};
    assign biased_sum = {sum_next[31], sum_next} + {scalar1_i[31], scalar1_i};
    assign maximum_next = (in_first_i || (input_a > maximum_q)) ? input_a : maximum_q;
    assign product_a_extended = {{14{product_a_q[35]}}, product_a_q};
    assign product_b_extended = {{14{product_b_q[35]}}, product_b_q};
    assign lane_data[8*lane+:8] = clamped_q;

    round_shift u_round_shift (
      .clk       (clk),
      .rst_ni    (rst_ni),
      .advance_i (advance),
      .data_i    (combined_q),
      .shift_i   (shift_q[2]),
      .data_o    (rounded)
    );

    // AVG: x = signed(high16)*2^17 + unsigned(low17). Each partial
    // multiply fits one DSP48E1, also used for one centered ADD input.
    always_ff @(posedge clk) begin
      if (rst_ni && advance) begin
        if (in_valid_i) begin
          if (op_i == OP_AVG) sum_q <= sum_next;
          if (op_i == OP_MAX) maximum_q <= maximum_next;
        end
        case (op_i)
          OP_ADD: begin
            operand_a_q <= {{9{centered_a[8]}}, centered_a};
            operand_b_q <= {{9{centered_b[8]}}, centered_b};
            mult_a_q    <= $signed({1'b0, scalar0_i[16:0]});
            mult_b_q    <= $signed({1'b0, scalar1_i[16:0]});
          end
          OP_AVG: begin
            operand_a_q <= $signed({1'b0, biased_sum[16:0]});
            operand_b_q <= {{2{biased_sum[32]}}, biased_sum[32:17]};
            mult_a_q    <= $signed({1'b0, scalar0_i[16:0]});
            mult_b_q    <= $signed({1'b0, scalar0_i[16:0]});
          end
          default: begin
            // MAX traverses the same rounding path exactly: (max*2) >> 1.
            operand_a_q <= {{10{maximum_next[7]}}, maximum_next};
            operand_b_q <= '0;
            mult_a_q    <= 18'sd2;
            mult_b_q    <= '0;
          end
        endcase
        product_a_q <= operand_a_q * mult_a_q;
        product_b_q <= operand_b_q * mult_b_q;
        combined_q <= product_a_extended +
            (avg_q2 ? (product_b_extended <<< 17) : product_b_extended);
        offset_q <= {rounded[49], rounded} + {{43{zp_q[5][7]}}, zp_q[5]};
        if (offset_q < $signed({{43{min_q[6][7]}}, min_q[6]})) begin
          clamped_q <= min_q[6];
        end else if (offset_q > $signed({{43{max_q[6][7]}}, max_q[6]})) begin
          clamped_q <= max_q[6];
        end else begin
          clamped_q <= offset_q[7:0];
        end
      end
    end
  end

  // Only validity and queue occupancy need resetting. A group's first beat
  // initializes its reduction state, including immediately after reset.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      valid_q      <= '0;
      skid_count_q <= '0;
    end else begin
      if (advance) begin
        valid_q <= {valid_q[LATENCY-3:0], in_valid_i && ((op_i == OP_ADD) || in_last_i)};
      end
      case ({
        skid_push, skid_pop
      })
        2'b10:   skid_count_q <= skid_count_q + 2'd1;
        2'b01:   skid_count_q <= skid_count_q - 2'd1;
        default: ;
      endcase
    end
  end

  // Two registered skid slots decouple input ready from downstream ready.
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
