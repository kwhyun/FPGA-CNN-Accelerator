`default_nettype none
// Independent weight/coefficient producer. Missing odd tiles are zero-filled;
// coefficients are retained until the drain releases their own semaphore.
module weight_coef_loader (
  input  wire          clk,
  input  wire          rst_ni,
  input  wire  [511:0] instr_i,
  input  wire  [255:0] plan_i,
  input  wire          desc_valid_i,
  input  wire  [671:0] desc_i,
  input  wire          bank_ready_i,
  input  wire          bank_i,
  input  wire          coef_ready_i,
  input  wire          coef_bank_i,
  input  wire          cmd_ready_i,
  input  wire          data_valid_i,
  input  wire  [ 63:0] data_i,
  input  wire          data_last_i,
  output wire          desc_ready_o,
  output wire          cmd_valid_o,
  output wire  [ 31:0] cmd_addr_o,
  output wire  [ 19:0] cmd_beats_o,
  output wire          data_ready_o,
  output wire          wr_en_o,
  output wire          wr_bank_o,
  output wire          wr_array_o,
  output wire  [  9:0] wr_addr_o,
  output wire  [ 63:0] wr_data_o,
  output wire          coef_wr_en_o,
  output wire          coef_wr_bank_o,
  output wire  [  5:0] coef_wr_addr_o,
  output logic         publish_o,
  output logic         coef_publish_o,
  output wire          idle_o
);
  wire [ 3:0] layer_type = instr_i[60+:4];
  wire [31:0] in2_base = instr_i[320+:32];
  wire        unused_instruction = ^instr_i;
  wire [23:0] plan_tiles_k = plan_i[32+:24];
  wire [15:0] plan_n_tiles = plan_i[56+:16];
  wire        unused_plan = ^plan_i;
  wire [31:0] block_q = desc_i[160+:32];
  wire [31:0] block_kb0 = desc_i[224+:32];
  wire [31:0] block_kb1 = desc_i[256+:32];
  wire [31:0] block_ch0 = desc_i[288+:32];
  wire [31:0] block_nch = desc_i[320+:32];
  wire [31:0] block_offset = desc_i[352+:32];
  wire [31:0] block_in_beats = desc_i[480+:32];
  wire [31:0] block_flags = desc_i[576+:32];
  wire [31:0] block_w_addr = desc_i[608+:32];
  wire [31:0] block_coef_addr = desc_i[640+:32];
  wire        unused_descriptor = ^desc_i;

  typedef enum logic [3:0] {
    IDLE,
    COEF_CMD,
    COEF_DATA,
    WEIGHT_CMD,
    WEIGHT_DATA,
    ZERO_FILL,
    NEXT_TILE,
    COMPLETE
  } state_t;
  state_t state_q;
  logic bank_q, coef_bank_q, array_q;
  logic [2:0] pp_q, q_q;
  logic [31:0] address_q, coef_address_q;
  logic [15:0] nt_q;
  logic [19:0] beats_q, count_q;
  logic [9:0] write_q, pair_base_q;
  logic [ 5:0] coef_write_q;
  logic [19:0] coef_beats_q;
  logic [19:0] nk_beats_q;
  logic [19:0] valid_dw_beats_q;
  wire         first_k = block_flags[0];
  wire         needs_coef = layer_type < 2 && first_k;
  wire         weight_data = state_q == WEIGHT_DATA;
  wire         zero_data = state_q == ZERO_FILL;
  wire         dw_second = (layer_type == 1) && (count_q >= nk_beats_q);
  assign idle_o = state_q == IDLE;
  assign desc_ready_o = idle_o && !publish_o && bank_ready_i && (!needs_coef || coef_ready_i);
  assign cmd_valid_o = (state_q == COEF_CMD) ||
      ((state_q == WEIGHT_CMD) && (layer_type != 0 || nt_q < plan_n_tiles));
  assign cmd_addr_o = (state_q == COEF_CMD) ? coef_address_q : address_q;
  assign cmd_beats_o = (state_q == COEF_CMD) ? coef_beats_q : beats_q;
  assign data_ready_o = (state_q == COEF_DATA) || weight_data;
  assign wr_en_o = zero_data || (weight_data && data_valid_i);
  assign wr_bank_o = bank_q;
  assign wr_array_o = (layer_type == 2) ? count_q[10] :
      ((layer_type == 1 && weight_data) ? dw_second : array_q);
  assign wr_addr_o = (layer_type == 2) ? count_q[9:0] :
      ((layer_type == 1 && weight_data) ? 10'(dw_second ? count_q - nk_beats_q : count_q) :
      write_q);
  assign wr_data_o = zero_data ? 64'b0 : data_i;
  assign coef_wr_en_o = state_q == COEF_DATA && data_valid_i;
  assign coef_wr_bank_o = coef_bank_q;
  assign coef_wr_addr_o = coef_write_q;
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      state_q        <= IDLE;
      publish_o      <= 1'b0;
      coef_publish_o <= 1'b0;
    end else begin
      publish_o      <= 1'b0;
      coef_publish_o <= 1'b0;
      case (state_q)
        IDLE:
        if (desc_valid_i && desc_ready_o) begin
          bank_q <= bank_i;
          coef_bank_q <= coef_bank_i;
          q_q <= block_q[2:0];
          pp_q <= 0;
          array_q <= 0;
          nt_q <= 16'(block_ch0 >> 3);
          address_q <= (layer_type == 2) ? in2_base + block_offset : block_w_addr;
          coef_address_q <= block_coef_addr;
          coef_beats_q <= 20'(block_nch);
          coef_write_q <= 0;
          write_q <= 0;
          pair_base_q <= 0;
          count_q <= 0;
          nk_beats_q <= (layer_type == 0) ? 20'((block_kb1 - block_kb0) << 3) : 20'(plan_tiles_k);
          beats_q <= (layer_type == 2) ? block_in_beats[19:0] :
              ((layer_type == 1) ? 20'(32'(plan_tiles_k) << (block_nch == 16 ? 1 : 0)) :
               20'((block_kb1 - block_kb0) << 3));
          valid_dw_beats_q <= 20'(32'(plan_tiles_k) << (block_nch == 16 ? 1 : 0));
          state_q <= needs_coef ? COEF_CMD : WEIGHT_CMD;
        end
        COEF_CMD: if (cmd_ready_i) state_q <= COEF_DATA;
        COEF_DATA:
        if (data_valid_i) begin
          coef_write_q <= coef_write_q + 1'b1;
          if (data_last_i) begin
            coef_publish_o <= 1'b1;
            state_q        <= WEIGHT_CMD;
          end
        end
        WEIGHT_CMD: begin
          if ((layer_type == 0) && nt_q >= plan_n_tiles) begin
            state_q <= ZERO_FILL;
            count_q <= 0;
          end else if (cmd_ready_i) state_q <= WEIGHT_DATA;
        end
        WEIGHT_DATA:
        if (data_valid_i) begin
          count_q <= count_q + 1'b1;
          write_q <= write_q + 1'b1;
          if (data_last_i) begin
            if (layer_type == 2) state_q <= COMPLETE;
            else if (layer_type == 1) begin
              if (valid_dw_beats_q == nk_beats_q) begin
                state_q <= ZERO_FILL;
                count_q <= 0;
                write_q <= 0;
                array_q <= 1'b1;
              end else state_q <= COMPLETE;
            end else state_q <= NEXT_TILE;
          end
        end
        ZERO_FILL: begin
          count_q <= count_q + 1'b1;
          write_q <= write_q + 1'b1;
          if (count_q + 1 == nk_beats_q) state_q <= (layer_type == 1) ? COMPLETE : NEXT_TILE;
        end
        NEXT_TILE: begin
          count_q   <= 0;
          nt_q      <= nt_q + 1'b1;
          address_q <= address_q + (32'(plan_tiles_k) << 6);
          array_q   <= !array_q;
          write_q   <= pair_base_q;
          state_q   <= WEIGHT_CMD;
          if (array_q) begin
            pp_q        <= pp_q + 1'b1;
            pair_base_q <= pair_base_q + 10'(nk_beats_q);
            write_q     <= pair_base_q + 10'(nk_beats_q);
            if (pp_q + 1'b1 == q_q) state_q <= COMPLETE;
          end
        end
        COMPLETE: begin
          publish_o <= 1'b1;
          state_q   <= IDLE;
        end
        default:  state_q <= IDLE;
      endcase
    end
  end
  wire unused_alias_bits = ^{block_q, block_in_beats, block_flags};
endmodule
`default_nettype wire
