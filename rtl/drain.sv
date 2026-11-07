`default_nettype none
// ACC/coefficient consumer and ordered output DMA. Both arrays are requantized
// together; pixel commands contain all valid channel tiles of the n-block.
module drain (
  input  wire          clk,
  input  wire          rst_ni,
  input  wire  [511:0] instr_i,
  input  wire  [255:0] plan_i,
  input  wire          desc_valid_i,
  input  wire  [671:0] desc_i,
  input  wire          acc_valid_i,
  input  wire          acc_bank_i,
  input  wire          coef_valid_i,
  input  wire          coef_bank_i,
  input  wire  [ 63:0] coef_rdata0_i,
  input  wire  [ 63:0] coef_rdata1_i,
  input  wire  [  1:0] acc_ready_i,
  input  wire  [  1:0] acc_rvalid_i,
  input  wire  [255:0] acc_rdata0_i,
  input  wire  [255:0] acc_rdata1_i,
  input  wire          vector_valid_i,
  input  wire  [ 63:0] vector_data_i,
  input  wire          cmd_ready_i,
  input  wire          data_ready_i,
  output wire          desc_ready_o,
  output wire          coef_rd_en_o,
  output wire          coef_bank_o,
  output wire  [  5:0] coef_addr0_o,
  output wire  [  5:0] coef_addr1_o,
  output wire  [  1:0] acc_rd_valid_o,
  output wire          acc_bank_o,
  output wire  [  8:0] acc_addr_o,
  output wire  [  1:0] acc_rready_o,
  output wire          vector_ready_o,
  output wire          vector_allow_o,
  output wire          cmd_valid_o,
  output wire  [ 31:0] cmd_addr_o,
  output wire  [ 19:0] cmd_beats_o,
  output wire          data_valid_o,
  output wire  [ 63:0] data_o,
  output logic         acc_release_o,
  output logic         coef_release_o,
  output wire          idle_o
);
  wire [ 3:0] layer_type = instr_i[60+:4];
  wire [11:0] c_out = instr_i[168+:12];
  wire [ 7:0] zp_out = instr_i[144+:8];
  wire [ 7:0] act_min = instr_i[136+:8];
  wire [ 7:0] act_max = instr_i[128+:8];
  wire        unused_instruction = ^instr_i;
  wire [ 0:0] plan_n_outer = plan_i[211+:1];
  wire        unused_plan = ^plan_i;
  wire [31:0] block_out_addr = desc_i[512+:32];
  wire [31:0] block_out_beats = desc_i[544+:32];
  wire        unused_descriptor = ^desc_i;

  typedef enum logic [3:0] {
    IDLE,
    COEF_REQ,
    COEF_RETURN,
    COMMAND,
    SEND_FIRST,
    SEND_SECOND,
    NEXT_PAIR,
    VECTOR
  } state_t;
  state_t state_q;
  logic acc_bank_q, coef_bank_q;
  logic [671:0] desc_q;
  wire  [ 31:0] active_m = desc_q[128+:32];
  wire  [ 31:0] active_nch = desc_q[320+:32];
  wire  [  3:0] active_flags = desc_q[576+:4];
  logic [ 31:0] address_q;
  logic [19:0] beats_q, vector_left_q;
  logic [  9:0] pixel_q;
  logic [  2:0] pp_q;
  logic [8:0] acc_addr_q;
  logic stream_active_q, request_finished_q;
  logic [9:0] request_pixel_q;
  logic [2:0] request_pair_q;
  logic [1:0] response_pair_q;
  wire request_last_pair = ((32'(request_pair_q) + 1) << 4) >= active_nch;
  wire response_last_pair = ((32'(response_pair_q) + 1) << 4) >= active_nch;
  wire response_second = ({29'b0, response_pair_q, 1'b1} << 3) < active_nch;
  wire request_fire = stream_active_q && !request_finished_q && (&acc_ready_i);
  logic [  5:0] coef_index_q;
  logic [511:0] coefficient_q[8];
  wire [1:0] rq_ready, rq_valid;
  wire [63:0] rq_data                                                               [2];
  wire [ 1:0] rq_out_ready;
  wire        joint_input = stream_active_q && (&acc_rvalid_i) && (&rq_ready);
  wire        valid_second = ({28'b0, pp_q, 1'b1} << 3) < active_nch;
  wire        last_pair = (({29'b0, pp_q} + 1) << 4) >= active_nch;
  wire        conv = layer_type < 2;
  wire        unused_active_descriptor = ^desc_q;
  assign idle_o = state_q == IDLE;
  assign desc_ready_o = idle_o && !acc_release_o && !coef_release_o && (!conv || (acc_valid_i
      && coef_valid_i));
  assign coef_rd_en_o = state_q == COEF_REQ;
  assign coef_bank_o = coef_bank_q;
  assign coef_addr0_o = coef_index_q;
  assign coef_addr1_o = coef_index_q + 1'b1;
  assign acc_rd_valid_o = {2{request_fire}};
  assign acc_bank_o = acc_bank_q;
  assign acc_addr_o = acc_addr_q;
  assign acc_rready_o = {2{joint_input}};
  assign vector_allow_o = state_q == VECTOR;
  assign vector_ready_o = state_q == VECTOR && data_ready_i;
  assign cmd_valid_o = state_q == COMMAND;
  assign cmd_addr_o = address_q;
  assign cmd_beats_o = beats_q;
  assign data_valid_o = (state_q == VECTOR) ? vector_valid_i :
      ((state_q == SEND_FIRST) ? (rq_valid[0] && (valid_second || rq_valid[1])) : ((state_q ==
      SEND_SECOND) && rq_valid[1]));
  assign data_o = (state_q == VECTOR) ? vector_data_i :
      ((state_q == SEND_FIRST) ? rq_data[0] : rq_data[1]);
  assign rq_out_ready[0] = state_q == SEND_FIRST && data_ready_i && (valid_second || rq_valid[1]);
  assign rq_out_ready[1] = data_ready_i && ((state_q == SEND_SECOND) ||
      (state_q == SEND_FIRST && !valid_second && rq_valid[0]));
  for (genvar array_idx = 0; array_idx < 2; array_idx++) begin : gen_requant
    quantization u_quantization (
      .clk(clk),
      .rst_ni(rst_ni),
      .zp_out_i(zp_out),
      .act_min_i(act_min),
      .act_max_i(act_max),
      .in_valid_i(joint_input),
      .in_acc_i(array_idx == 0 ? acc_rdata0_i : acc_rdata1_i),
      .in_coef_i((array_idx == 1 && !response_second) ? 512'b0 :
          coefficient_q[{response_pair_q, 1'b0} + 3'(array_idx)]),
      .out_ready_i(rq_out_ready[array_idx]),
      .in_ready_o(rq_ready[array_idx]),
      .out_valid_o(rq_valid[array_idx]),
      .out_data_o(rq_data[array_idx])
    );
  end
  // The small coefficient cache holds one n-block (at most 64 channels).
  // It is loaded only from the BRAM bank owned by this drain operation.
  always_ff @(posedge clk) begin
    if (state_q == COEF_RETURN) begin
      coefficient_q[coef_index_q[5:3]][64*coef_index_q[2:0]+:64]          <= coef_rdata0_i;
      coefficient_q[coef_index_q[5:3]][64*(32'(coef_index_q[2:0])+1)+:64] <= coef_rdata1_i;
    end
    if (!rst_ni) begin
      state_q        <= IDLE;
      acc_release_o  <= 1'b0;
      coef_release_o <= 1'b0;
      stream_active_q <= 1'b0;
    end else begin
      acc_release_o  <= 1'b0;
      coef_release_o <= 1'b0;
      // Request and response counters advance independently of the output
      // command path. Accumulator and requant credits bound the read-ahead.
      if (request_fire) begin
        if (request_last_pair) begin
          request_pair_q <= 0;
          request_pixel_q <= request_pixel_q + 1'b1;
          acc_addr_q <= 9'(request_pixel_q + 1'b1);
          if (32'(request_pixel_q) + 1 == active_m) request_finished_q <= 1'b1;
        end else begin
          request_pair_q <= request_pair_q + 1'b1;
          acc_addr_q <= acc_addr_q + active_m[8:0];
        end
      end
      if (joint_input) response_pair_q <= response_last_pair ? 2'b0 : response_pair_q + 1'b1;
      case (state_q)
        IDLE:
        if (desc_valid_i && desc_ready_o) begin
          desc_q        <= desc_i;
          acc_bank_q    <= acc_bank_i;
          coef_bank_q   <= coef_bank_i;
          address_q     <= block_out_addr;
          beats_q       <= block_out_beats[19:0];
          vector_left_q <= block_out_beats[19:0];
          pixel_q       <= 0;
          pp_q          <= 0;
          acc_addr_q    <= 0;
          coef_index_q  <= 0;
          request_pixel_q <= 0;
          request_pair_q <= 0;
          response_pair_q <= 0;
          request_finished_q <= 1'b0;
          state_q       <= conv ? COEF_REQ : COMMAND;
        end
        COEF_REQ: state_q <= COEF_RETURN;
        COEF_RETURN: begin
          coef_index_q <= coef_index_q + 2;
          if (32'(coef_index_q) + 2 >= active_nch) stream_active_q <= 1'b1;
          state_q      <= (32'(coef_index_q) + 2 >= active_nch) ? COMMAND : COEF_REQ;
        end
        COMMAND: if (cmd_ready_i) state_q <= conv ? SEND_FIRST : VECTOR;
        SEND_FIRST:
        if (rq_valid[0] && (valid_second || rq_valid[1]) && data_ready_i) state_q <=
        valid_second ? SEND_SECOND : NEXT_PAIR;
        SEND_SECOND: if (rq_valid[1] && data_ready_i) state_q <= NEXT_PAIR;
        NEXT_PAIR: begin
          if (last_pair) begin
            pp_q       <= 0;
            pixel_q    <= pixel_q + 1'b1;
            address_q  <= address_q + ((32'(c_out) + 7) & 32'hffff_fff8);
            if (32'(pixel_q) + 1 == active_m) begin
              state_q        <= IDLE;
              acc_release_o  <= 1'b1;
              stream_active_q <= 1'b0;
              coef_release_o <= !plan_n_outer || active_flags[2];
            end else state_q <= COMMAND;
          end else begin
            pp_q       <= pp_q + 1'b1;
            state_q    <= SEND_FIRST;
          end
        end
        VECTOR:
        if (vector_valid_i && data_ready_i) begin
          vector_left_q <= vector_left_q - 1'b1;
          if (vector_left_q == 1) state_q <= IDLE;
        end
        default: state_q <= IDLE;
      endcase
    end
  end
  wire unused_alias_bits = ^{block_out_beats, active_flags};
endmodule
`default_nettype wire
