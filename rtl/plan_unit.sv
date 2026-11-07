`default_nettype none
// Sequential planner for HwConfig() defaults. Geometry ports match ISA widths.
// Counts are 16 bits (channel quantities), 24 bits (tiles/blocks), or 3 bits (q).
// Arithmetic uses 32-bit quotients and 64-bit products/costs, including signed
// input-row sums: the Python boundary formula can count negative loaded rows for
// all-padding windows. No combinational divider, multiplier, RAM, or DSP is used.
//
// Plan members retain Python's defaults when unused: k_blks is zero outside
// CONV; MAXPOOL m_blk is zero; ADD/POOL n_pairs and tiles_k are zero. Additional
// interface quantities are derived as documented: q=1 outside CONV, n_nblk is
// ceil(n_pairs/q), and avg_groups uses min(groups, 65536/(H*W*8)), WITHOUT the
// max(1, ...) clamp in tlm.py. MAXPOOL preserves plan_pool's R=1 fallback.
// cs/kc_n/n_tiles are provided for every operation; avg_groups is otherwise zero.
// The row-fit helper's 1x1 subsampling applies to DW/MAX as in tiling.py, while
// subsample_o is asserted only for CONV, as specified by the interface.
//
// start_i is accepted only while idle. Inputs are captured on that edge; busy
// starts are ignored. done_o pulses for one cycle; results/error then hold until
// the next accepted start. Reset aborts work; datapath registers are not reset.
// Latency is data dependent: 32 cycles/division, at most 33 cycles/product,
// at most 512 row-fit steps and one cycle/output block for the CONV cost sum.
module plan_unit (
  input wire clk,
  input wire rst_ni,

  // Execution control input
  input wire start_i,

  // Layer configuration input
  input wire [3:0] layer_type_i,
  input wire [3:0] kh_i,
  input wire [3:0] kw_i,
  input wire [3:0] stride_i,
  input wire [3:0] pad_t_i,
  input wire [3:0] pad_l_i,
  input wire [3:0] pad_b_i,
  input wire [3:0] pad_r_i,
  input wire       in_dense_i,

  // Geometry input
  input wire [11:0] h_in_i,
  input wire [11:0] w_in_i,
  input wire [11:0] h_out_i,
  input wire [11:0] w_out_i,
  input wire [11:0] c_in_i,
  input wire [11:0] c_out_i,

  // Status and plan output
  output logic        done_o,
  output logic        error_o,
  output logic [15:0] cs_o,
  output logic [15:0] kc_n_o,
  output logic [23:0] tiles_k_o,
  output logic [15:0] n_tiles_o,
  output logic [15:0] n_pairs_o,
  output logic [ 2:0] pairs_per_blk_o,
  output logic [15:0] n_nblk_o,
  output logic [15:0] tiles_per_kblk_o,
  output logic [23:0] k_blks_o,
  output logic [15:0] rows_per_blk_o,
  output logic [23:0] m_blk_o,
  output logic [23:0] n_mblk_o,
  output logic        n_outer_o,
  output logic        subsample_o,
  output logic [15:0] avg_groups_o
);
  localparam logic [3:0] CONV = 4'd0;
  localparam logic [3:0] DWCONV = 4'd1;
  localparam logic [3:0] ADD = 4'd2;
  localparam logic [3:0] MAXPOOL = 4'd3;
  localparam logic [3:0] AVGPOOL = 4'd4;

  typedef enum logic [5:0] {
    IDLE,
    INIT,
    KC_DONE,
    TILES_DONE,
    Q_PICK,
    R_INIT,
    ROW_BYTES,
    ROWS_CAP,
    SPAN_DONE,
    ROW_SEARCH,
    ROW_FAILED,
    ROW_DONE,
    M_DONE,
    MBLKS_DONE,
    KBLKS_DONE,
    NBLKS_DONE,
    END_DONE,
    SUM_ROWS,
    ONCE_REQ,
    ONCE_DONE,
    LEFT_DONE,
    WEIGHT_DONE,
    COST_DONE,
    PIXELS_REQ,
    PIXELS_DONE,
    AVG_GROUPS,
    NBYTES_DONE,
    MULTIPLY,
    DIVIDE,
    FINISH
  } state_t;
  state_t state_q, return_state_q;

  logic [3:0] layer_type_q;
  logic [31:0] kh_q, kw_q, stride_q, pad_t_q;
  logic [31:0] h_in_q, w_in_q, h_out_q, w_out_q, padded_h_q, padded_cs_q;
  logic subsample_q;
  logic [31:0] row_bytes_q, capacity_rows_q, span_rows_q, pixels_q;
  logic [31:0] block_oh_q, advance_q;
  logic signed [31:0] block_lo_q, block_hi_q, end_hi_q, loaded_rows_q;
  logic signed [31:0] clipped_lo, clipped_hi;
  logic [63:0] left_cost_q;
  logic [31:0] q_choice;

  // Shared shift/add product engine. A two's-complement lhs times an unsigned
  // rhs also works modulo 2^64, allowing the signed Python traffic comparison.
  logic [63:0] mul_lhs_q, mul_acc_q;
  logic [31:0] mul_rhs_q;

  // Shared restoring divider: one quotient bit per cycle, no '/' operators.
  logic [31:0] div_quotient_q, div_denominator_q, div_remainder_q;
  logic [32:0] div_trial;
  logic [ 5:0] div_count_q;

  // Horizontal pads do not enter any of the reference planner's formulas.
  wire         unused_horizontal_pads = ^{pad_l_i, pad_r_i};

  task automatic launch_mul(input logic [63:0] lhs, input logic [31:0] rhs,
                            input state_t continuation);
    mul_lhs_q      <= lhs;
    mul_rhs_q      <= rhs;
    mul_acc_q      <= '0;
    return_state_q <= continuation;
    state_q        <= MULTIPLY;
  endtask

  task automatic launch_div(input logic [31:0] numerator, input logic [31:0] denominator,
                            input state_t continuation);
    div_quotient_q    <= numerator;
    div_denominator_q <= denominator;
    div_remainder_q   <= '0;
    div_count_q       <= '0;
    return_state_q    <= continuation;
    state_q           <= DIVIDE;
  endtask

  // Small selection/clipping paths between registered arithmetic operations.
  always_comb begin
    q_choice = 32'd1;
    if (layer_type_q == CONV) begin
      q_choice = 32'd4;
      if (32'(n_pairs_o) < q_choice) q_choice = 32'(n_pairs_o);
      if (div_quotient_q < q_choice) q_choice = div_quotient_q;
      if (q_choice == 0) q_choice = 32'd1;
    end
    clipped_lo = block_lo_q < 0 ? 32'sd0 : block_lo_q;
    clipped_hi = block_hi_q < end_hi_q ? block_hi_q : end_hi_q;
    if (clipped_hi > $signed(h_in_q) - 1) clipped_hi = $signed(h_in_q) - 1;
    div_trial = {div_remainder_q, div_quotient_q[31]};
  end

  // Only control/status require reset. Every datapath value is initialized by
  // its launching state before use, including after an interrupted operation.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      state_q <= IDLE;
      done_o  <= 1'b0;
      error_o <= 1'b0;
    end else begin
      done_o <= 1'b0;
      case (state_q)
        IDLE: begin
          if (start_i) begin
            layer_type_q <= layer_type_i;
            kh_q         <= 32'(kh_i);
            kw_q         <= 32'(kw_i);
            stride_q     <= 32'(stride_i);
            pad_t_q      <= 32'(pad_t_i);
            h_in_q       <= 32'(h_in_i);
            w_in_q       <= 32'(w_in_i);
            h_out_q      <= 32'(h_out_i);
            w_out_q      <= 32'(w_out_i);
            padded_h_q   <= 32'(h_in_i) + 32'(pad_t_i) + 32'(pad_b_i);
            padded_cs_q  <= (32'(c_in_i) + 32'd7) & 32'hffff_fff8;
            subsample_q  <= kh_i == 1 && kw_i == 1 && stride_i > 1;
            cs_o         <= in_dense_i ? 16'(c_in_i) : ((16'(c_in_i) + 16'd7) & 16'hfff8);
            n_tiles_o    <= (16'(c_out_i) + 16'd7) >> 3;
            n_pairs_o    <= '0;
            if (layer_type_i == CONV) n_pairs_o <= (16'(c_out_i) + 16'd15) >> 4;
            if (layer_type_i == DWCONV) n_pairs_o <= (16'(c_in_i) + 16'd15) >> 4;
            kc_n_o           <= '0;
            tiles_k_o        <= '0;
            pairs_per_blk_o  <= 3'd1;
            n_nblk_o         <= '0;
            tiles_per_kblk_o <= 16'd128;
            k_blks_o         <= '0;
            rows_per_blk_o   <= '0;
            m_blk_o          <= '0;
            n_mblk_o         <= '0;
            n_outer_o        <= 1'b0;
            subsample_o      <= layer_type_i == CONV && kh_i == 1 && kw_i == 1 && stride_i > 1;
            avg_groups_o     <= '0;
            error_o          <= 1'b0;
            state_q          <= INIT;
            if (layer_type_i > AVGPOOL || kh_i == 0 || kw_i == 0 || stride_i == 0 ||
                h_in_i == 0 || w_in_i == 0 || h_out_i == 0 || w_out_i == 0 ||
                c_in_i == 0 || c_out_i == 0) begin
              error_o <= 1'b1;
              state_q <= FINISH;
            end
          end
        end
        INIT: launch_mul(64'(kw_q), 32'(cs_o), KC_DONE);
        KC_DONE: begin
          kc_n_o <= 16'((mul_acc_q + 7) >> 3);
          if (layer_type_q == CONV) begin
            launch_mul(64'(kh_q), 32'((mul_acc_q + 7) >> 3), TILES_DONE);
          end else if (layer_type_q == DWCONV) begin
            launch_mul(64'(kh_q), kw_q, TILES_DONE);
          end else if (layer_type_q == MAXPOOL) begin
            state_q <= TILES_DONE;
          end else begin
            state_q <= PIXELS_REQ;
          end
        end
        TILES_DONE: begin
          if (layer_type_q != MAXPOOL) tiles_k_o <= 24'(mul_acc_q);
          launch_div(32'd512, w_out_q, Q_PICK);
        end
        Q_PICK: begin
          pairs_per_blk_o <= 3'(q_choice);
          launch_div(div_quotient_q, q_choice, R_INIT);
        end
        R_INIT: begin
          rows_per_blk_o <= 16'(div_quotient_q < h_out_q ? div_quotient_q : h_out_q);
          if (div_quotient_q == 0) begin
            state_q <= ROW_FAILED;
          end else begin
            launch_mul(64'(subsample_q ? w_out_q : w_in_q),
                       layer_type_q == CONV ? 32'(cs_o) : padded_cs_q, ROW_BYTES);
          end
        end
        ROW_BYTES: begin
          row_bytes_q <= 32'(mul_acc_q);
          launch_div(32'd65536, 32'(mul_acc_q), ROWS_CAP);
        end
        ROWS_CAP: begin
          capacity_rows_q <= div_quotient_q;
          launch_mul(64'(rows_per_blk_o) - 1, stride_q, SPAN_DONE);
        end
        SPAN_DONE: begin
          span_rows_q <= 32'(mul_acc_q) + kh_q;
          state_q     <= ROW_SEARCH;
        end
        ROW_SEARCH: begin
          if (rows_per_blk_o == 0) begin
            state_q <= ROW_FAILED;
          end else if (subsample_q ? 32'(rows_per_blk_o) <= capacity_rows_q :
                       (span_rows_q < padded_h_q ? span_rows_q : padded_h_q) <=
                       capacity_rows_q) begin
            state_q <= ROW_DONE;
          end else begin
            rows_per_blk_o <= rows_per_blk_o - 1'b1;
            span_rows_q    <= span_rows_q - stride_q;
          end
        end
        ROW_FAILED: begin
          if (layer_type_q == MAXPOOL) begin
            rows_per_blk_o <= 16'd1;
            state_q        <= ROW_DONE;
          end else begin
            error_o <= 1'b1;
            state_q <= FINISH;
          end
        end
        ROW_DONE: begin
          if (layer_type_q == MAXPOOL) begin
            launch_div(h_out_q + 32'(rows_per_blk_o) - 1, 32'(rows_per_blk_o), MBLKS_DONE);
          end else begin
            launch_mul(64'(rows_per_blk_o), w_out_q, M_DONE);
          end
        end
        M_DONE: begin
          m_blk_o <= 24'(mul_acc_q);
          launch_div(h_out_q + 32'(rows_per_blk_o) - 1, 32'(rows_per_blk_o), MBLKS_DONE);
        end
        MBLKS_DONE: begin
          n_mblk_o <= 24'(div_quotient_q);
          if (layer_type_q == CONV) begin
            launch_mul(64'(tiles_k_o), 32'(pairs_per_blk_o), KBLKS_DONE);
          end else if (layer_type_q == DWCONV) begin
            n_nblk_o <= n_pairs_o;
            state_q  <= FINISH;
          end else begin
            state_q <= FINISH;
          end
        end
        KBLKS_DONE: begin
          k_blks_o <= 24'((mul_acc_q + 127) >> 7);
          case (pairs_per_blk_o)
            3'd1: tiles_per_kblk_o <= 16'd128;
            3'd2: tiles_per_kblk_o <= 16'd64;
            3'd3: tiles_per_kblk_o <= 16'd42;
            default: tiles_per_kblk_o <= 16'd32;
          endcase
          launch_div(32'(n_pairs_o) + 32'(pairs_per_blk_o) - 1, 32'(pairs_per_blk_o), NBLKS_DONE);
        end
        NBLKS_DONE: begin
          n_nblk_o <= 16'(div_quotient_q);
          if (k_blks_o == 1 && n_mblk_o > 1) begin
            if (subsample_q) begin
              loaded_rows_q <= $signed(h_out_q);
              state_q       <= ONCE_REQ;
            end else begin
              launch_mul(64'(h_out_q) - 1, stride_q, END_DONE);
            end
          end else begin
            state_q <= FINISH;
          end
        end
        END_DONE: begin
          end_hi_q      <= $signed(32'(mul_acc_q)) - $signed(pad_t_q) + $signed(kh_q) - 1;
          block_lo_q    <= -$signed(pad_t_q);
          block_hi_q    <= $signed(span_rows_q) - $signed(pad_t_q) - 1;
          advance_q     <= span_rows_q + stride_q - kh_q;
          block_oh_q    <= '0;
          loaded_rows_q <= '0;
          state_q       <= SUM_ROWS;
        end
        SUM_ROWS: begin
          loaded_rows_q <= loaded_rows_q + clipped_hi - clipped_lo + 1;
          if (block_oh_q + 32'(rows_per_blk_o) >= h_out_q) begin
            state_q <= ONCE_REQ;
          end else begin
            block_oh_q <= block_oh_q + 32'(rows_per_blk_o);
            block_lo_q <= block_lo_q + $signed(advance_q);
            block_hi_q <= block_hi_q + $signed(advance_q);
          end
        end
        ONCE_REQ: launch_mul(64'(loaded_rows_q), row_bytes_q, ONCE_DONE);
        ONCE_DONE: launch_mul(mul_acc_q, 32'(n_nblk_o) - 1, LEFT_DONE);
        LEFT_DONE: begin
          left_cost_q <= mul_acc_q;
          launch_mul(64'(n_tiles_o), 32'(tiles_k_o), WEIGHT_DONE);
        end
        WEIGHT_DONE: launch_mul(mul_acc_q << 6, 32'(n_mblk_o) - 1, COST_DONE);
        COST_DONE: begin
          // t_n < t_m iff input_once*(n_nblk-1) < weight_once*(n_mblk-1).
          n_outer_o <= $signed(left_cost_q) < $signed(mul_acc_q);
          state_q   <= FINISH;
        end
        PIXELS_REQ: launch_mul(64'(h_in_q), w_in_q, PIXELS_DONE);
        PIXELS_DONE: begin
          pixels_q <= 32'(mul_acc_q);
          if (layer_type_q == AVGPOOL) begin
            launch_div(32'd8192, 32'(mul_acc_q), AVG_GROUPS);
          end else begin
            launch_mul(mul_acc_q, padded_cs_q, NBYTES_DONE);
          end
        end
        AVG_GROUPS: begin
          avg_groups_o <= 16'(div_quotient_q < (padded_cs_q >> 3) ?
                              div_quotient_q : (padded_cs_q >> 3));
          launch_mul(64'(pixels_q), padded_cs_q, NBYTES_DONE);
        end
        NBYTES_DONE: begin
          if (layer_type_q == ADD) n_mblk_o <= 24'((mul_acc_q + 16383) >> 14);
          else n_mblk_o <= 24'((mul_acc_q + 65535) >> 16);
          state_q <= FINISH;
        end
        MULTIPLY: begin
          if (mul_rhs_q == 0) begin
            state_q <= return_state_q;
          end else begin
            if (mul_rhs_q[0]) mul_acc_q <= mul_acc_q + mul_lhs_q;
            mul_lhs_q <= mul_lhs_q << 1;
            mul_rhs_q <= mul_rhs_q >> 1;
          end
        end
        DIVIDE: begin
          if (div_trial >= {1'b0, div_denominator_q}) begin
            div_remainder_q <= 32'(div_trial - {1'b0, div_denominator_q});
            div_quotient_q  <= {div_quotient_q[30:0], 1'b1};
          end else begin
            div_remainder_q <= 32'(div_trial);
            div_quotient_q  <= {div_quotient_q[30:0], 1'b0};
          end
          if (div_count_q == 31) state_q <= return_state_q;
          else div_count_q <= div_count_q + 1'b1;
        end
        FINISH: begin
          done_o  <= 1'b1;
          state_q <= IDLE;
        end
        default: state_q <= IDLE;
      endcase
    end
  end
endmodule
`default_nettype wire
