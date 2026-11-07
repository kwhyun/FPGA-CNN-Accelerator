`default_nettype none
module input_loader (
  input  wire          clk,
  input  wire          rst_ni,
  input  wire  [511:0] instr_i,
  input  wire  [255:0] plan_i,
  input  wire          desc_valid_i,
  input  wire  [671:0] desc_i,
  input  wire          bank_ready_i,
  input  wire          bank_i,
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
  output wire  [ 12:0] wr_addr_o,
  output wire  [ 63:0] wr_data_o,
  output logic         publish_o,
  output wire          idle_o
);
  wire [ 3:0] layer_type = instr_i[60+:4];
  wire [ 3:0] stride = instr_i[48+:4];
  wire [11:0] w_in = instr_i[104+:12];
  wire [11:0] w_out = instr_i[80+:12];
  wire        unused_instruction = ^instr_i;
  wire [15:0] plan_cs = plan_i[0+:16];
  wire [ 0:0] plan_subsample = plan_i[212+:1];
  wire        unused_plan = ^plan_i;
  wire [31:0] block_m = desc_i[128+:32];
  wire [31:0] block_ng = desc_i[384+:32];
  wire [31:0] block_npix = desc_i[416+:32];
  wire [31:0] block_in_addr = desc_i[448+:32];
  wire [31:0] block_in_beats = desc_i[480+:32];
  wire        unused_descriptor = ^desc_i;

  typedef enum logic [1:0] {
    IDLE,
    SETUP,
    COMMAND,
    RECEIVE
  } state_t;
  state_t        state_q;
  logic          bank_q;
  logic   [11:0] pitch_factor_q;
  logic   [31:0] pitch_term_q;
  logic [31:0] address_q, row_origin_q, row_step_q, pixel_step_q;
  logic [19:0] beats_q;
  logic [12:0] write_q;
  logic [31:0] remaining_q;
  logic [11:0] column_q;
  wire         split_pixels = plan_subsample || layer_type == 4;
  assign idle_o       = state_q == IDLE;
  assign desc_ready_o = idle_o && !publish_o && bank_ready_i;
  assign cmd_valid_o  = state_q == COMMAND;
  assign cmd_addr_o   = address_q;
  assign cmd_beats_o  = beats_q;
  assign data_ready_o = state_q == RECEIVE;
  assign wr_en_o      = data_valid_i && data_ready_o;
  assign wr_bank_o    = bank_q;
  assign wr_addr_o    = write_q;
  assign wr_data_o    = data_i;
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      state_q   <= IDLE;
      publish_o <= 1'b0;
    end else begin
      publish_o <= 1'b0;
      case (state_q)
        IDLE:
        if (desc_valid_i && desc_ready_o) begin
          bank_q <= bank_i;
          address_q <= block_in_addr;
          row_origin_q <= block_in_addr;
          write_q <= 0;
          column_q <= 0;
          pixel_step_q <= (layer_type == 4) ? 32'(plan_cs) :
              (32'(plan_cs) << (stride == 2 ? 1 : 0));
          // Row pitch is supplied by the already planned input block: use
          // registered shift/add setup below for the subsampling row wrap.
          row_step_q <= 0;
          pitch_factor_q <= w_in;
          pitch_term_q <= 32'(plan_cs) << (stride == 2 ? 1 : 0);
          remaining_q <= split_pixels ? ((layer_type == 4) ? block_npix : block_m) : 1;
          beats_q <= 20'(split_pixels ? ((layer_type == 4) ? block_ng :
              32'(plan_cs >> 3)) : block_in_beats);
          state_q <= SETUP;
        end
        SETUP: begin
          if (pitch_factor_q == 0) state_q <= COMMAND;
          else begin
            if (pitch_factor_q[0]) row_step_q <= row_step_q + pitch_term_q;
            pitch_factor_q <= pitch_factor_q >> 1;
            pitch_term_q   <= pitch_term_q << 1;
          end
        end
        COMMAND: if (cmd_ready_i) state_q <= RECEIVE;
        RECEIVE:
        if (data_valid_i) begin
          write_q <= write_q + 1'b1;
          if (data_last_i) begin
            if (remaining_q == 1) begin
              state_q   <= IDLE;
              publish_o <= 1'b1;
            end else begin
              remaining_q <= remaining_q - 1;
              address_q   <= address_q + pixel_step_q;
              if (plan_subsample) begin
                if (column_q + 1 == w_out) begin
                  column_q     <= 0;
                  row_origin_q <= row_origin_q + row_step_q;
                  address_q    <= row_origin_q + row_step_q;
                end else column_q <= column_q + 1'b1;
              end
              state_q <= COMMAND;
            end
          end
        end
        default: state_q <= IDLE;
      endcase
    end
  end
endmodule
`default_nettype wire
