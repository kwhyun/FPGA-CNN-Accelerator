`default_nettype none
// Instruction fetch, plan, execute, and response fence. Soft reset finishes the
// active layer before resetting engines: no accepted AXI transaction is lost.
module layer_control (
  input  wire          clk,
  input  wire          rst_ni,
  input  wire          start_i,
  input  wire          soft_reset_i,
  input  wire  [ 31:0] instr_base_i,
  input  wire  [ 31:0] instr_count_i,
  input  wire          cmd_ready_i,
  input  wire          data_valid_i,
  input  wire  [ 63:0] data_i,
  input  wire          plan_done_i,
  input  wire          plan_error_i,
  input  wire          block_done_i,
  input  wire          engines_idle_i,
  input  wire          axi_error_i,
  output wire          cmd_valid_o,
  output wire  [ 31:0] cmd_addr_o,
  output wire          data_ready_o,
  output logic [511:0] instr_o,
  output logic         plan_start_o,
  output logic         block_start_o,
  output logic         reset_engines_o,
  output wire          busy_o,
  output logic         done_o,
  output logic         error_o,
  output logic [ 31:0] layer_index_o,
  output logic [ 31:0] error_code_o
);
  typedef enum logic [3:0] {
    IDLE,
    FETCH,
    RECEIVE,
    DECODE,
    PLAN,
    EXECUTE,
    FENCE,
    RESET_WAIT
  } state_t;
  state_t state_q;
  logic [31:0] address_q, count_q;
  logic [2:0] word_q;
  logic       abort_q;
  assign cmd_valid_o  = state_q == FETCH;
  assign cmd_addr_o   = address_q;
  assign data_ready_o = state_q == RECEIVE;
  assign busy_o       = state_q != IDLE;
  always_ff @(posedge clk) begin
    if (data_valid_i && data_ready_o) instr_o[64*word_q+:64] <= data_i;
    if (!rst_ni) begin
      state_q         <= IDLE;
      plan_start_o    <= 0;
      block_start_o   <= 0;
      reset_engines_o <= 0;
      done_o          <= 0;
      error_o         <= 0;
      layer_index_o   <= 0;
      error_code_o    <= 0;
      abort_q         <= 0;
    end else begin
      plan_start_o    <= 0;
      block_start_o   <= 0;
      reset_engines_o <= 0;
      done_o          <= 0;
      error_o         <= 0;
      if (soft_reset_i) abort_q <= 1;
      case (state_q)
        IDLE: begin
          if (soft_reset_i) state_q <= RESET_WAIT;
          else if (start_i) begin
            address_q     <= instr_base_i;
            count_q       <= instr_count_i;
            layer_index_o <= 0;
            error_code_o  <= 0;
            abort_q       <= 0;
            word_q        <= 0;
            if (instr_count_i == 0) done_o <= 1;
            else state_q <= FETCH;
          end
        end
        FETCH:   if (cmd_ready_i) state_q <= RECEIVE;
        RECEIVE:
        if (data_valid_i) begin
          word_q <= word_q + 1'b1;
          if (word_q == 7) state_q <= DECODE;
        end
        DECODE: begin
          if (abort_q) state_q <= RESET_WAIT;
          else if (axi_error_i || instr_o[63:60] > 4) begin
            error_code_o <= axi_error_i ? 3 : 2;
            error_o      <= 1;
            state_q      <= IDLE;
          end else begin
            plan_start_o <= 1;
            state_q      <= PLAN;
          end
        end
        PLAN:
        if (plan_done_i) begin
          if (abort_q) state_q <= RESET_WAIT;
          else if (plan_error_i) begin
            error_code_o <= 1;
            error_o      <= 1;
            state_q      <= IDLE;
          end else begin
            block_start_o <= 1;
            state_q       <= EXECUTE;
          end
        end
        EXECUTE: if (block_done_i) state_q <= FENCE;
        FENCE:
        if (engines_idle_i) begin
          if (abort_q) state_q <= RESET_WAIT;
          else if (axi_error_i) begin
            error_code_o <= 3;
            error_o      <= 1;
            state_q      <= IDLE;
          end else if (layer_index_o + 1 == count_q) begin
            done_o  <= 1;
            state_q <= IDLE;
          end else begin
            layer_index_o <= layer_index_o + 1;
            address_q     <= address_q + 64;
            word_q        <= 0;
            state_q       <= FETCH;
          end
        end
        RESET_WAIT:
        if (engines_idle_i) begin
          reset_engines_o <= 1;
          error_code_o    <= 0;
          state_q         <= IDLE;
          abort_q         <= 0;
        end
        default: state_q <= IDLE;
      endcase
    end
  end
endmodule
`default_nettype wire
