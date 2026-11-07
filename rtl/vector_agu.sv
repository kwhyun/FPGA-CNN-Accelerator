`default_nettype none
// One issue/cycle after setup. Four FIFO entries reserve space for the one
// outstanding synchronous RAM response. MAX setup uses shift/add, with no DSPs.
module vector_agu (
  input wire clk,
  input wire rst_ni,
  input wire start_i,

  // Configuration remains stable through done_o.
  input wire [ 1:0] op_i,
  input wire [ 3:0] stride_i,
  input wire [ 3:0] kh_i,
  input wire [ 3:0] kw_i,
  input wire [ 3:0] pad_t_i,
  input wire [ 3:0] pad_l_i,
  input wire [11:0] h_in_i,
  input wire [11:0] w_in_i,
  input wire [11:0] w_out_i,
  input wire [11:0] lo_i,
  input wire [11:0] oh0_i,
  input wire [11:0] rows_i,
  input wire [ 9:0] groups_i,
  input wire [15:0] npix_i,
  input wire [15:0] nrows_i,
  input wire [ 7:0] pad_value_i,

  // Synchronous scratchpad responses.
  input wire [63:0] in_rdata0_i,
  input wire [63:0] in_rdata1_i,
  input wire [63:0] w_rdata0_i,
  input wire [63:0] w_rdata1_i,
  input wire        v_ready_i,

  output logic        done_o,
  output logic        busy_o,
  output wire  [ 1:0] in_rd_en_o,
  output wire  [12:0] in_rd_addr0_o,
  output wire  [12:0] in_rd_addr1_o,
  output wire         w_rd_en_o,
  output wire  [10:0] w_rd_addr_o,

  // Vector input stream.
  output wire        v_valid_o,
  output wire [63:0] v_a_o,
  output wire [63:0] v_b_o,
  output wire        v_first_o,
  output wire        v_last_o
);
  localparam logic [1:0] OP_ADD = 2'd0;
  localparam logic [1:0] OP_MAX = 2'd1;
  typedef enum logic [2:0] {
    IDLE,
    PITCH,
    BASE,
    INIT,
    RUN,
    DRAIN
  } state_t;
  state_t             state_q;

  // Setup products are computed a bit per cycle, outside the issue path.
  logic        [ 4:0] setup_bit_q;
  logic        [31:0] pitch_q;
  logic        [31:0] origin_ih_q;
  logic        [31:0] left_groups_q;
  logic        [31:0] stride_groups_q;
  logic        [31:0] stride_pitch_q;
  logic        [31:0] factor_q;
  logic               factor_negative_q;
  logic signed [31:0] base_q;
  wire signed  [31:0] initial_delta;
  wire signed  [31:0] initial_ih;
  wire signed  [31:0] pitch_signed;
  wire signed  [31:0] stride_signed;
  wire signed  [31:0] group_signed;
  assign initial_ih    = $signed(origin_ih_q) - $signed({28'd0, pad_t_i});
  assign initial_delta = initial_ih - $signed({20'd0, lo_i});
  assign pitch_signed  = $signed(pitch_q);
  assign stride_signed = $signed({28'd0, stride_i});
  assign group_signed  = $signed({22'd0, groups_i});

  // MAX loop coordinates and nested address origins. Legal blocks fit 8192
  // rows; signed intermediates also represent negative padding addresses.
  logic        [11:0] output_row_q;
  logic        [11:0] output_col_q;
  logic        [ 9:0] group_q;
  logic        [ 3:0] kernel_row_q;
  logic        [ 3:0] kernel_col_q;
  logic        [15:0] pixel_q;
  logic        [15:0] add_row_q;
  logic signed [31:0] row_ih_q;
  logic signed [31:0] ih_q;
  logic signed [31:0] pixel_iw_q;
  logic signed [31:0] iw_q;
  logic signed [31:0] row_addr_q;
  logic signed [31:0] pixel_addr_q;
  logic signed [31:0] tap_row_addr_q;
  logic signed [31:0] addr_q;
  wire signed  [31:0] next_row_addr;
  wire signed  [31:0] next_pixel_addr;
  assign next_row_addr   = row_addr_q + $signed(stride_pitch_q);
  assign next_pixel_addr = pixel_addr_q + $signed(stride_groups_q);

  // Request metadata travels alongside the BRAM read, including synthetic pads.
  wire         issue;
  wire         pop;
  wire         pad;
  wire         first;
  wire         last;
  wire         final_row;
  logic        pending_q;
  logic        pending_pad_q;
  logic        pending_bank_q;
  logic        pending_first_q;
  logic        pending_last_q;
  logic        pending_final_q;
  logic [ 1:0] head_q;
  logic [ 1:0] tail_q;
  logic [ 2:0] count_q;
  logic [63:0] fifo_a_q        [4];
  logic [63:0] fifo_b_q        [4];
  logic        fifo_first_q    [4];
  logic        fifo_last_q     [4];
  logic        fifo_final_q    [4];

  assign v_valid_o = rst_ni && (count_q != 0);
  assign v_a_o = fifo_a_q[head_q];
  assign v_b_o = fifo_b_q[head_q];
  assign v_first_o = fifo_first_q[head_q];
  assign v_last_o = fifo_last_q[head_q];
  assign pop = v_valid_o && v_ready_i;
  assign issue = rst_ni && (state_q == RUN) &&
      (({1'b0, count_q} + {3'd0, pending_q} < 4'd4) || pop);
  assign pad = (op_i == OP_MAX) && ((ih_q < 0) || (ih_q >= $signed(
      {20'd0, h_in_i}
  )) || (iw_q < 0) || (iw_q >= $signed(
      {20'd0, w_in_i}
  )));
  assign first = (op_i == OP_ADD) || ((op_i == OP_MAX) ?
      ((kernel_row_q == 0) && (kernel_col_q == 0)) : (pixel_q == 0));
  assign last = (op_i == OP_ADD) || ((op_i == OP_MAX) ?
      ((kernel_row_q == kh_i - 1'b1) && (kernel_col_q == kw_i - 1'b1)) :
      (pixel_q == npix_i - 1'b1));
  assign final_row = (op_i == OP_ADD) ? (add_row_q == nrows_i - 1'b1) :
      last && (group_q == groups_i - 1'b1) && ((op_i != OP_MAX) ||
      ((output_row_q == rows_i - 1'b1) && (output_col_q == w_out_i - 1'b1)));
  assign in_rd_en_o = {1'b0, issue && !pad};
  assign in_rd_addr0_o = (op_i == OP_ADD) ? add_row_q[12:0] : addr_q[12:0];
  assign in_rd_addr1_o = '0;
  assign w_rd_en_o = issue && (op_i == OP_ADD);
  assign w_rd_addr_o = add_row_q[10:0];

  // Only control state resets. Data slots and loop state initialize on start.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      state_q   <= IDLE;
      busy_o    <= 1'b0;
      done_o    <= 1'b0;
      pending_q <= 1'b0;
      head_q    <= '0;
      tail_q    <= '0;
      count_q   <= '0;
    end else begin
      done_o    <= 1'b0;
      pending_q <= issue;
      if (issue) begin
        pending_pad_q   <= pad;
        pending_bank_q  <= add_row_q[10];
        pending_first_q <= first;
        pending_last_q  <= last;
        pending_final_q <= final_row;
      end
      if (pending_q) begin
        fifo_a_q[tail_q] <= pending_pad_q ? {8{pad_value_i}} : in_rdata0_i;
        fifo_b_q[tail_q] <= (op_i == OP_ADD) ? (pending_bank_q ? w_rdata1_i : w_rdata0_i) : 64'd0;
        fifo_first_q[tail_q] <= pending_first_q;
        fifo_last_q[tail_q] <= pending_last_q;
        fifo_final_q[tail_q] <= pending_final_q;
        tail_q <= tail_q + 1'b1;
      end
      if (pop) head_q <= head_q + 1'b1;
      case ({
        pending_q, pop
      })
        2'b10:   count_q <= count_q + 1'b1;
        2'b01:   count_q <= count_q - 1'b1;
        default: ;
      endcase
      if (pop && fifo_final_q[head_q]) begin
        busy_o  <= 1'b0;
        done_o  <= 1'b1;
        state_q <= IDLE;
      end

      case (state_q)
        IDLE: begin
          if (start_i) begin
            busy_o          <= 1'b1;
            add_row_q       <= '0;
            pixel_q         <= '0;
            group_q         <= '0;
            addr_q          <= '0;
            setup_bit_q     <= '0;
            pitch_q         <= '0;
            origin_ih_q     <= '0;
            left_groups_q   <= '0;
            stride_groups_q <= '0;
            state_q         <= (op_i == OP_MAX) ? PITCH : RUN;
          end
        end
        PITCH: begin
          if ((setup_bit_q < 10) && groups_i[setup_bit_q[3:0]]) begin
            pitch_q <= pitch_q + ({20'd0, w_in_i} << setup_bit_q);
          end
          if (setup_bit_q < 4) begin
            if (stride_i[setup_bit_q[1:0]]) begin
              origin_ih_q     <= origin_ih_q + ({20'd0, oh0_i} << setup_bit_q);
              stride_groups_q <= stride_groups_q + ({22'd0, groups_i} << setup_bit_q);
            end
            if (pad_l_i[setup_bit_q[1:0]]) begin
              left_groups_q <= left_groups_q + ({22'd0, groups_i} << setup_bit_q);
            end
          end
          setup_bit_q <= setup_bit_q + 1'b1;
          if (setup_bit_q == 10) begin
            factor_negative_q <= initial_delta < 0;
            factor_q <= (initial_delta < 0) ? $unsigned(-initial_delta) : $unsigned(initial_delta);
            base_q <= '0;
            stride_pitch_q <= '0;
            setup_bit_q <= '0;
            state_q <= BASE;
          end
        end
        BASE: begin
          if (factor_q[setup_bit_q]) begin
            base_q <= factor_negative_q ? base_q - (pitch_signed <<< setup_bit_q) :
                base_q + (pitch_signed <<< setup_bit_q);
          end
          if ((setup_bit_q < 4) && stride_i[setup_bit_q[1:0]]) begin
            stride_pitch_q <= stride_pitch_q + (pitch_q << setup_bit_q);
          end
          setup_bit_q <= setup_bit_q + 1'b1;
          if (setup_bit_q == 17) state_q <= INIT;
        end
        INIT: begin
          output_row_q   <= '0;
          output_col_q   <= '0;
          kernel_row_q   <= '0;
          kernel_col_q   <= '0;
          row_ih_q       <= initial_ih;
          ih_q           <= initial_ih;
          pixel_iw_q     <= -$signed({28'd0, pad_l_i});
          iw_q           <= -$signed({28'd0, pad_l_i});
          row_addr_q     <= base_q;
          pixel_addr_q   <= base_q - $signed(left_groups_q);
          tap_row_addr_q <= base_q - $signed(left_groups_q);
          addr_q         <= base_q - $signed(left_groups_q);
          state_q        <= RUN;
        end
        RUN: begin
          if (issue) begin
            if (final_row) state_q <= DRAIN;
            if (op_i == OP_ADD) begin
              add_row_q <= add_row_q + 1'b1;
            end else if (op_i != OP_MAX) begin
              if (last) begin
                pixel_q <= '0;
                group_q <= group_q + 1'b1;
                addr_q  <= $signed({22'd0, group_q}) + 32'sd1;
              end else begin
                pixel_q <= pixel_q + 1'b1;
                addr_q  <= addr_q + group_signed;
              end
            end else if (kernel_col_q != kw_i - 1'b1) begin
              kernel_col_q <= kernel_col_q + 1'b1;
              iw_q         <= iw_q + 32'sd1;
              addr_q       <= addr_q + group_signed;
            end else begin
              kernel_col_q <= '0;
              iw_q         <= pixel_iw_q;
              if (kernel_row_q != kh_i - 1'b1) begin
                kernel_row_q   <= kernel_row_q + 1'b1;
                ih_q           <= ih_q + 32'sd1;
                tap_row_addr_q <= tap_row_addr_q + pitch_signed;
                addr_q         <= tap_row_addr_q + pitch_signed;
              end else begin
                kernel_row_q <= '0;
                ih_q         <= row_ih_q;
                if (group_q != groups_i - 1'b1) begin
                  group_q        <= group_q + 1'b1;
                  tap_row_addr_q <= pixel_addr_q + $signed({22'd0, group_q}) + 32'sd1;
                  addr_q         <= pixel_addr_q + $signed({22'd0, group_q}) + 32'sd1;
                end else begin
                  group_q <= '0;
                  if (output_col_q != w_out_i - 1'b1) begin
                    output_col_q   <= output_col_q + 1'b1;
                    pixel_iw_q     <= pixel_iw_q + stride_signed;
                    iw_q           <= pixel_iw_q + stride_signed;
                    pixel_addr_q   <= next_pixel_addr;
                    tap_row_addr_q <= next_pixel_addr;
                    addr_q         <= next_pixel_addr;
                  end else begin
                    output_col_q   <= '0;
                    output_row_q   <= output_row_q + 1'b1;
                    row_ih_q       <= row_ih_q + stride_signed;
                    ih_q           <= row_ih_q + stride_signed;
                    pixel_iw_q     <= -$signed({28'd0, pad_l_i});
                    iw_q           <= -$signed({28'd0, pad_l_i});
                    row_addr_q     <= next_row_addr;
                    pixel_addr_q   <= next_row_addr - $signed(left_groups_q);
                    tap_row_addr_q <= next_row_addr - $signed(left_groups_q);
                    addr_q         <= next_row_addr - $signed(left_groups_q);
                  end
                end
              end
            end
          end
        end
        default: ;
      endcase
    end
  end

  // Port 1 is reserved: all three operations read one input row per beat.
  wire unused_input_port = ^in_rdata1_i;
endmodule
`default_nettype wire
