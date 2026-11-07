`default_nettype none
// Owns compute-side resource lifetimes. Array completion waits 20 clocks after
// the final activation and then checks both accumulator write pipelines.
module compute_issue (
  input  wire          clk,
  input  wire          rst_ni,
  input  wire  [511:0] instr_i,
  input  wire  [255:0] plan_i,
  input  wire          desc_valid_i,
  input  wire  [671:0] desc_i,
  input  wire          in_valid_i,
  input  wire          in_bank_i,
  input  wire          w_valid_i,
  input  wire          w_bank_i,
  input  wire          acc_ready_i,
  input  wire          acc_bank_i,
  input  wire          vector_allow_i,
  input  wire          agu_done_i,
  input  wire  [  1:0] acc_busy_i,
  output wire          desc_ready_o,
  output logic [671:0] active_desc_o,
  output logic         in_bank_o,
  output logic         w_bank_o,
  output logic         acc_bank_o,
  output logic         conv_start_o,
  output logic         vec_start_o,
  output logic         in_release_o,
  output logic         w_release_o,
  output logic         acc_publish_o,
  output wire          idle_o
);
  wire [  3:0] layer_type = instr_i[60+:4];
  wire         unused_instruction = ^instr_i;
  wire [  0:0] plan_n_outer = plan_i[211+:1];
  wire         unused_plan = ^plan_i;

  wire [671:0] desc_active = active_desc_o;
  wire [  3:0] flags = desc_active[576+:4];
  wire [  3:0] incoming_flags = desc_i[576+:4];
  typedef enum logic [1:0] {
    IDLE,
    RUN,
    FLUSH
  } state_t;
  state_t       state_q;
  logic   [4:0] flush_q;
  wire          conv = layer_type < 2;
  wire          need_weight = layer_type < 3;
  wire          need_acc = conv && incoming_flags[0];
  assign idle_o = state_q == IDLE;
  assign desc_ready_o = idle_o && !in_release_o && !w_release_o && !acc_publish_o && in_valid_i
      && (!need_weight || w_valid_i) &&
      (!need_acc || acc_ready_i) && (conv || vector_allow_i);
  wire unused_descriptor = ^{desc_i, desc_active};
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      state_q       <= IDLE;
      conv_start_o  <= 1'b0;
      vec_start_o   <= 1'b0;
      in_release_o  <= 1'b0;
      w_release_o   <= 1'b0;
      acc_publish_o <= 1'b0;
    end else begin
      conv_start_o  <= 1'b0;
      vec_start_o   <= 1'b0;
      in_release_o  <= 1'b0;
      w_release_o   <= 1'b0;
      acc_publish_o <= 1'b0;
      case (state_q)
        IDLE:
        if (desc_valid_i && desc_ready_o) begin
          active_desc_o <= desc_i;
          in_bank_o     <= in_bank_i;
          w_bank_o      <= w_bank_i;
          if (incoming_flags[0]) acc_bank_o <= acc_bank_i;
          conv_start_o <= conv;
          vec_start_o  <= !conv;
          state_q      <= RUN;
        end
        RUN:
        if (agu_done_i) begin
          in_release_o <= !conv || (flags[1] && (plan_n_outer || flags[3]));
          w_release_o  <= need_weight && (!plan_n_outer || flags[2]);
          if (conv) begin
            flush_q <= 20;
            state_q <= FLUSH;
          end else state_q <= IDLE;
        end
        FLUSH: begin
          if (flush_q != 0) flush_q <= flush_q - 1'b1;
          else if (acc_busy_i == 0) begin
            acc_publish_o <= flags[1];
            state_q       <= IDLE;
          end
        end
        default: state_q <= IDLE;
      endcase
    end
  end
  wire unused_alias_bits = ^{flags, incoming_flags};
endmodule
`default_nettype wire
