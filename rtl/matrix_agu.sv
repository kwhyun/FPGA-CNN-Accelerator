`default_nettype none
// Incremental block sequencer. Configuration remains stable through done_o.
// Address issue -> registered BRAM address -> synchronous RAM -> registered
// gather/mask -> activation FIFO takes three edges. The eight-entry activation
// queue prefetches across tile boundaries, hiding BRAM latency behind shadow
// waits. No row-loop multiply or divide is used. A 16-entry weight queue decouples RAM reads from shadow stalls.
module matrix_agu (
  input wire clk,
  input wire rst_ni,

  // Execution and stable block configuration.
  input wire        start_i,
  input wire        dw_i,
  input wire        in_dense_i,
  input wire        subsample_i,
  input wire [ 3:0] stride_i,
  input wire [ 3:0] kh_i,
  input wire [ 3:0] kw_i,
  input wire [ 3:0] pad_t_i,
  input wire [ 3:0] pad_l_i,
  input wire [11:0] w_in_i,
  input wire [11:0] w_out_i,
  input wire [11:0] cs_i,
  input wire [11:0] kc_n_i,
  input wire [11:0] oh0_i,
  input wire [11:0] rows_i,
  input wire [11:0] lo_i,
  input wire [11:0] hi_i,
  input wire [ 9:0] m_blk_i,
  input wire [ 2:0] q_i,
  input wire [10:0] pair_i,
  input wire [15:0] kb0_i,
  input wire [15:0] kb1_i,
  input wire        first_kblk_i,
  input wire [ 7:0] pad_value_i,

  // Synchronous input and weight RAM responses.
  input wire [63:0] in_rdata0_i,
  input wire [63:0] in_rdata1_i,
  input wire [63:0] w_rdata0_i,
  input wire [63:0] w_rdata1_i,
  input wire        w_ready0_i,
  input wire        w_ready1_i,
  input wire        shadow_full0_i,
  input wire        shadow_full1_i,

  // Execution status and registered RAM address outputs.
  output logic        done_o,
  output wire         busy_o,
  output logic [ 1:0] in_rd_en_o,
  output logic [12:0] in_rd_addr0_o,
  output logic [12:0] in_rd_addr1_o,
  output wire         w_rd_en_o,
  output wire  [ 9:0] w_rd_addr_o,

  // Joint weight stream and always-accepted activation stream.
  output wire         w_valid_o,
  output wire  [63:0] w_row0_o,
  output wire  [63:0] w_row1_o,
  output logic        a_valid_o,
  output logic [63:0] a_row0_o,
  output logic [63:0] a_row1_o,
  output logic        a_swap_o,
  output logic [ 9:0] a_tag_o
);
  typedef enum logic [3:0] {
    IDLE,
    PRODUCTS,
    DIVIDE,
    BYTE_DIVIDE,
    LANES,
    RELATIVE,
    MULTIPLY,
    ORIGIN,
    RUN,
    FINISH
  } state_t;
  state_t state_q;

  // Block-only geometry setup; the restoring divider runs for sixteen clocks.
  logic [15:0] div_quot_q;
  logic [15:0] div_rem_q;
  logic [4:0] div_count_q;
  logic [11:0] tile_cols_q;
  wire [16:0] div_trial = {div_rem_q[15:0], div_quot_q[15]};
  wire div_subtract = div_trial >= {5'b0, tile_cols_q};
  logic signed [31:0] row_bytes_q;
  logic signed [31:0] pixel_step_q;
  logic signed [31:0] row_step_q;
  logic signed [31:0] first_ih_q;
  logic signed [31:0] origin_q;
  logic signed [31:0] tile_wrap_q;
  logic signed [31:0] relative_row_q;
  logic signed [31:0] row_product_q;
  logic signed [31:0] pad_bytes_q;
  logic signed [31:0] span_bytes_q;
  logic signed [31:0] wrap_bytes_q;
  logic signed [31:0] column_bias_q;
  logic [15:0] kr_q;
  logic [11:0] span_q;
  logic [15:0] byte_div_q;
  logic [11:0] byte_rem_q;
  logic [3:0] lane_setup_q;
  logic [11:0] lane_kc_q[8];
  logic [11:0] lane_rem_q[8];
  logic [11:0] lane_kc_zero_q[8];
  logic [11:0] lane_rem_zero_q[8];
  logic [11:0] zero_kc_q;
  logic [11:0] zero_rem_q;
  logic [11:0] step_kc_q;
  logic [11:0] step_rem_q;
  logic [9:0] weight_pair_step_q;

  // Tile/m row counters and incremental byte address / horizontal coordinate.
  logic [15:0] tile_q;
  logic [2:0] pp_q;
  logic [9:0] pp_acc_q;
  logic [9:0] m_q;
  logic [11:0] ow_q;
  logic [11:0] oh_count_q;
  logic signed [31:0] ih_q;
  logic signed [31:0] iw_q;
  logic signed [31:0] row_origin_q;
  logic signed [31:0] byte_addr_q;
  wire span_last = span_q + 1'b1 == tile_cols_q;
  wire tile_last_pair = dw_i || (pp_q + 1'b1 == q_i);
  wire tile_last = tile_last_pair && ((tile_q + 1'b1 == kb1_i) ||
      ((kr_q + 1'b1 == {12'b0, kh_i}) && span_last));
  wire row_last = (m_q + 1'b1 == m_blk_i) ||
      ((ow_q + 1'b1 == w_out_i) && (oh_count_q + 1'b1 == rows_i));
  wire row_accum = !(first_kblk_i && (tile_q == kb0_i));
  wire [8:0] acc_address = 9'(pp_acc_q + m_q);
  wire signed [31:0] next_origin = origin_q +
      (subsample_i ? 32'sd8 : (span_last ? tile_wrap_q :
      (dw_i ? $signed(
      {20'b0, cs_i}
  ) : 32'sd8)));
  wire signed [31:0] next_first_ih = first_ih_q + ((span_last && !subsample_i) ? 32'sd1 : 32'sd0);
  wire signed [31:0] group_bytes = $signed({17'b0, pair_i, 4'b0});

  // The byte masks travel alongside both synchronous RAM address stages.
  logic input_valid_q;
  logic input_valid_q2;
  logic [7:0] mask_q;
  logic [7:0] mask_q2;
  logic [2:0] shift_q;
  logic [2:0] shift_q2;
  logic [9:0] tag_q;
  logic [9:0] tag_q2;
  logic swap_q;
  logic swap_q2;
  logic last_q;
  logic last_q2;
  logic assembly_valid_q;
  logic [139:0] activation_fifo_q[8];
  logic [2:0] activation_write_q;
  logic [2:0] activation_read_q;
  logic [3:0] activation_reserved_q;
  logic [3:0] activation_count_q;
  logic [63:0] assembly_row0_q;
  logic [63:0] assembly_row1_q;
  logic [9:0] assembly_tag_q;
  logic assembly_swap_q;
  logic assembly_last_q;
  wire activation_take = a_valid_o;
  wire row_issue = (state_q == RUN) && ((activation_reserved_q < 8) || activation_take);
  wire activation_last = activation_fifo_q[activation_read_q][138];

  assign a_valid_o = rst_ni && (activation_count_q != 0) &&
      (!a_swap_o || (shadow_full0_i && shadow_full1_i));
  assign a_row0_o = activation_fifo_q[activation_read_q][63:0];
  assign a_row1_o = activation_fifo_q[activation_read_q][127:64];
  assign a_tag_o = activation_fifo_q[activation_read_q][137:128];
  assign a_swap_o = activation_fifo_q[activation_read_q][139];
  logic       [  7:0] row_mask;
  wire signed [ 31:0] read_byte0 = byte_addr_q + (dw_i ? group_bytes : 32'sd0);
  wire signed [ 31:0] read_byte1 = dw_i ? read_byte0 + 32'sd8 : ((read_byte0 >>> 3) + 32'sd1) <<< 3;
  wire        [127:0] gather_shifted = {in_rdata1_i, in_rdata0_i} >> {shift_q2, 3'b0};

  // Safe physical addresses even when dense gathers straddle either bank edge.
  function automatic logic [12:0] bank_address(input logic signed [31:0] byte_address);
    if ((byte_address < 0) || (byte_address >= 65536)) return 13'b0;
    return byte_address[15:3];
  endfunction

  // Logical padding is independent of the physical gather address. The dense
  // final tile can have a different horizontal coordinate for every byte.
  // Loaded rows are the inclusive interval lo_i..hi_i; subsampled banks have
  // already selected their pixels and bypass logical padding entirely.
  always_comb begin
    for (int lane = 0; lane < 8; lane++) begin
      row_mask[lane] = subsample_i ||
          ((ih_q >= $signed({20'b0, lo_i})) && (ih_q <= $signed({20'b0, hi_i})) &&
           (iw_q + $signed({20'b0, lane_kc_q[lane]}) >= 0) &&
           (iw_q + $signed({20'b0, lane_kc_q[lane]}) < $signed({20'b0, w_in_i})) &&
           (lane_kc_q[lane] < {8'b0, kw_i}) && (read_byte0 + lane >= 0) &&
           (read_byte0 + lane < 65536));
    end
  end

  // Read-ahead FIFO: reservation count includes the one RAM response in flight.
  // Both arrays see a beat only when both can accept it, preventing divergence.
  logic [127:0] weight_fifo_q[16];
  logic [3:0] weight_write_q;
  logic [3:0] weight_read_q;
  logic [4:0] weight_reserved_q;
  logic weight_pending_q;
  logic [2:0] weight_pending_beat_q;
  logic [2:0] weight_beat_q;
  logic [2:0] weight_pp_q;
  logic [15:0] weight_tile_q;
  logic [9:0] weight_address_q;
  logic [9:0] weight_base_q;
  logic [9:0] weight_pair_base_q;
  logic weight_finished_q;
  wire weight_take = w_valid_o;
  wire weight_issue = ((state_q == RUN) || (state_q == FINISH)) && !weight_finished_q &&
      ((weight_reserved_q < 16) || weight_take);
  logic [63:0] weight_expand0;
  logic [63:0] weight_expand1;

  assign busy_o = state_q != IDLE;
  assign w_rd_en_o = weight_issue;
  assign w_rd_addr_o = weight_address_q;
  assign w_valid_o = rst_ni && (weight_reserved_q > 5'(weight_pending_q)) &&
      w_ready0_i && w_ready1_i;
  assign w_row0_o = weight_fifo_q[weight_read_q][63:0];
  assign w_row1_o = weight_fifo_q[weight_read_q][127:64];

  always_comb begin
    weight_expand0 = w_rdata0_i;
    weight_expand1 = w_rdata1_i;
    if (dw_i) begin
      weight_expand0                             = '0;
      weight_expand1                             = '0;
      weight_expand0[8*weight_pending_beat_q+:8] = w_rdata0_i[8*weight_pending_beat_q+:8];
      weight_expand1[8*weight_pending_beat_q+:8] = w_rdata1_i[8*weight_pending_beat_q+:8];
    end
  end

  // FIFO storage and geometry datapath are deliberately not reset.
  always_ff @(posedge clk) begin
    if (weight_pending_q) begin
      weight_fifo_q[weight_write_q] <= {weight_expand1, weight_expand0};
    end
    weight_pending_beat_q <= weight_beat_q;
    if (row_issue) begin
      in_rd_addr0_o <= bank_address(read_byte0);
      in_rd_addr1_o <= bank_address(read_byte1);
      mask_q        <= row_mask;
      shift_q       <= in_dense_i ? read_byte0[2:0] : 3'b0;
      tag_q         <= {row_accum, acc_address};
      swap_q        <= m_q == 0;
      last_q        <= row_last && tile_last;
    end
    mask_q2         <= mask_q;
    shift_q2        <= shift_q;
    tag_q2          <= tag_q;
    swap_q2         <= swap_q;
    last_q2         <= last_q;
    assembly_swap_q <= swap_q2;
    assembly_tag_q  <= tag_q2;
    assembly_last_q <= last_q2;
    if (assembly_valid_q) begin
      activation_fifo_q[activation_write_q] <= {
        assembly_swap_q, assembly_last_q, assembly_tag_q, assembly_row1_q, assembly_row0_q
      };
    end
    for (int lane = 0; lane < 8; lane++) begin
      assembly_row0_q[8*lane+:8] <= mask_q2[lane] ?
          (in_dense_i ? gather_shifted[8*lane+:8] : in_rdata0_i[8*lane+:8]) : pad_value_i;
      assembly_row1_q[8*lane+:8] <= mask_q2[lane] ?
          (dw_i ? in_rdata1_i[8*lane+:8] :
          (in_dense_i ? gather_shifted[8*lane+:8] : in_rdata0_i[8*lane+:8])) : pad_value_i;
    end
  end

  // Setup arithmetic is enabled only once per block. The divider avoids a long
  // combinational / path; the byte quotient requires at most kw subtracts.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      state_q               <= IDLE;
      done_o                <= 1'b0;
      input_valid_q         <= 1'b0;
      input_valid_q2        <= 1'b0;
      in_rd_en_o            <= '0;
      assembly_valid_q      <= 1'b0;
      activation_write_q    <= '0;
      activation_read_q     <= '0;
      activation_reserved_q <= '0;
      activation_count_q    <= '0;
      weight_write_q        <= '0;
      weight_read_q         <= '0;
      weight_reserved_q     <= '0;
      weight_pending_q      <= 1'b0;
      weight_finished_q     <= 1'b1;
    end else begin
      done_o           <= 1'b0;
      in_rd_en_o       <= {2{row_issue}};
      input_valid_q    <= row_issue;
      input_valid_q2   <= input_valid_q;
      assembly_valid_q <= input_valid_q2;
      case ({
        row_issue, activation_take
      })
        2'b10:   activation_reserved_q <= activation_reserved_q + 1'b1;
        2'b01:   activation_reserved_q <= activation_reserved_q - 1'b1;
        default: ;
      endcase
      case ({
        assembly_valid_q, activation_take
      })
        2'b10:   activation_count_q <= activation_count_q + 1'b1;
        2'b01:   activation_count_q <= activation_count_q - 1'b1;
        default: ;
      endcase
      if (assembly_valid_q) activation_write_q <= activation_write_q + 1'b1;
      if (activation_take) activation_read_q <= activation_read_q + 1'b1;
      if (activation_take && activation_last) begin
        done_o  <= 1'b1;
        state_q <= IDLE;
      end

      weight_pending_q <= weight_issue;
      case ({
        weight_issue, weight_take
      })
        2'b10:   weight_reserved_q <= weight_reserved_q + 1'b1;
        2'b01:   weight_reserved_q <= weight_reserved_q - 1'b1;
        default: ;
      endcase
      if (weight_pending_q) weight_write_q <= weight_write_q + 1'b1;
      if (weight_take) weight_read_q <= weight_read_q + 1'b1;
      if (weight_issue) begin
        weight_beat_q <= weight_beat_q + 1'b1;
        if (!dw_i) weight_address_q <= weight_address_q + 1'b1;
        if (weight_beat_q == 7) begin
          if (dw_i || (weight_pp_q + 1'b1 == q_i)) begin
            weight_pp_q        <= '0;
            weight_pair_base_q <= '0;
            weight_tile_q      <= weight_tile_q + 1'b1;
            weight_base_q      <= weight_base_q + 10'd8;
            weight_address_q   <= dw_i ? 10'(weight_tile_q + 1'b1) : weight_base_q + 10'd8;
            if (weight_tile_q + 1'b1 == kb1_i) weight_finished_q <= 1'b1;
          end else begin
            weight_pp_q        <= weight_pp_q + 1'b1;
            weight_pair_base_q <= weight_pair_base_q + weight_pair_step_q[9:0];
            weight_address_q   <= weight_base_q + weight_pair_base_q + weight_pair_step_q[9:0];
          end
        end
      end

      case (state_q)
        IDLE:
        if (start_i) begin
          row_bytes_q        <= 32'(w_in_i) * 32'(cs_i);
          pixel_step_q       <= 32'(stride_i) * 32'(cs_i);
          first_ih_q         <= 32'(oh0_i) * 32'(stride_i) - 32'(pad_t_i);
          tile_cols_q        <= dw_i ? {8'b0, kw_i} : kc_n_i;
          div_quot_q         <= kb0_i;
          div_rem_q          <= '0;
          div_count_q        <= 16;
          weight_pair_step_q <= 10'((kb1_i - kb0_i) << 3);
          tile_q             <= kb0_i;
          pp_q               <= '0;
          pp_acc_q           <= '0;
          m_q                <= '0;
          ow_q               <= '0;
          oh_count_q         <= '0;
          weight_tile_q      <= kb0_i;
          weight_pp_q        <= '0;
          weight_beat_q      <= '0;
          weight_base_q      <= '0;
          weight_address_q   <= dw_i ? kb0_i[9:0] : 10'b0;
          weight_pair_base_q <= '0;
          weight_finished_q  <= 1'b0;
          state_q            <= PRODUCTS;
        end
        PRODUCTS: begin
          row_step_q <= row_bytes_q * 32'(stride_i);
          state_q    <= DIVIDE;
        end
        DIVIDE: begin
          div_quot_q  <= {div_quot_q[14:0], div_subtract};
          div_rem_q   <= 16'(div_subtract ? div_trial - {5'b0, tile_cols_q} : div_trial);
          div_count_q <= div_count_q - 1'b1;
          if (div_count_q == 1) begin
            kr_q         <= {div_quot_q[14:0], div_subtract};
            span_q       <= 12'(div_subtract ? div_trial - {5'b0, tile_cols_q} : div_trial);
            state_q      <= BYTE_DIVIDE;
            byte_div_q   <= '0;
            byte_rem_q   <= 12'((div_subtract ? div_trial - {5'b0, tile_cols_q} : div_trial) << 3);
            zero_kc_q    <= '0;
            zero_rem_q   <= '0;
            lane_setup_q <= '0;
            step_kc_q    <= '0;
            step_rem_q   <= 8;
          end
        end
        BYTE_DIVIDE: begin
          if (byte_rem_q >= cs_i) begin
            byte_rem_q <= byte_rem_q - cs_i;
            byte_div_q <= byte_div_q + 1'b1;
          end else if (step_rem_q >= cs_i) begin
            step_rem_q <= step_rem_q - cs_i;
            step_kc_q  <= step_kc_q + 1'b1;
          end else begin
            state_q <= LANES;
          end
        end
        LANES: begin
          lane_kc_q[lane_setup_q[2:0]]       <= dw_i ? span_q : byte_div_q[11:0];
          lane_rem_q[lane_setup_q[2:0]]      <= byte_rem_q;
          lane_kc_zero_q[lane_setup_q[2:0]]  <= dw_i ? 12'b0 : zero_kc_q;
          lane_rem_zero_q[lane_setup_q[2:0]] <= zero_rem_q;
          if (byte_rem_q + 1'b1 == cs_i) begin
            byte_rem_q <= '0;
            byte_div_q <= byte_div_q + 1'b1;
          end else byte_rem_q <= byte_rem_q + 1'b1;
          if (zero_rem_q + 1'b1 == cs_i) begin
            zero_rem_q <= '0;
            zero_kc_q  <= zero_kc_q + 1'b1;
          end else zero_rem_q <= zero_rem_q + 1'b1;
          lane_setup_q <= lane_setup_q + 1'b1;
          if (lane_setup_q == 7) begin
            first_ih_q <= first_ih_q + $signed({16'b0, kr_q});
            state_q    <= RELATIVE;
          end
        end
        RELATIVE: begin
          relative_row_q <= first_ih_q - $signed({20'b0, lo_i});
          pad_bytes_q    <= 32'(pad_l_i) * 32'(cs_i);
          span_bytes_q   <= dw_i ? 32'(span_q) * 32'(cs_i) : $signed({17'b0, span_q, 3'b0});
          wrap_bytes_q   <= dw_i ? (32'(kw_i) - 1) * 32'(cs_i) : (32'(kc_n_i) - 1) * 8;
          state_q        <= MULTIPLY;
        end
        MULTIPLY: begin
          row_product_q <= relative_row_q * row_bytes_q;
          column_bias_q <= span_bytes_q - pad_bytes_q;
          tile_wrap_q   <= row_bytes_q - wrap_bytes_q;
          state_q       <= ORIGIN;
        end
        ORIGIN: begin
          origin_q     <= subsample_i ? span_bytes_q : row_product_q + column_bias_q;
          state_q      <= RUN;
          row_origin_q <= subsample_i ? span_bytes_q : row_product_q + column_bias_q;
          byte_addr_q  <= subsample_i ? span_bytes_q : row_product_q + column_bias_q;
          ih_q         <= first_ih_q;
          iw_q         <= -32'(pad_l_i);
        end
        RUN: begin
          if (row_issue) begin
            if (row_last) begin
              m_q        <= '0;
              ow_q       <= '0;
              oh_count_q <= '0;
              iw_q       <= -32'(pad_l_i);
              if (tile_last) state_q <= FINISH;
              if (tile_last_pair) begin
                pp_q         <= '0;
                pp_acc_q     <= '0;
                tile_q       <= tile_q + 1'b1;
                origin_q     <= next_origin;
                row_origin_q <= next_origin;
                byte_addr_q  <= next_origin;
                first_ih_q   <= next_first_ih;
                ih_q         <= next_first_ih;
                if (span_last) begin
                  span_q <= '0;
                  kr_q   <= kr_q + 1'b1;
                end else span_q <= span_q + 1'b1;
                for (int lane = 0; lane < 8; lane++) begin
                  if (span_last) begin
                    lane_kc_q[lane]  <= lane_kc_zero_q[lane];
                    lane_rem_q[lane] <= lane_rem_zero_q[lane];
                  end else if (dw_i) begin
                    lane_kc_q[lane] <= lane_kc_q[lane] + 1'b1;
                  end else if ({1'b0, lane_rem_q[lane]} + {1'b0, step_rem_q} >= {1'b0, cs_i}) begin
                    lane_kc_q[lane]  <= lane_kc_q[lane] + step_kc_q + 1'b1;
                    lane_rem_q[lane] <= lane_rem_q[lane] + step_rem_q - cs_i;
                  end else begin
                    lane_kc_q[lane]  <= lane_kc_q[lane] + step_kc_q;
                    lane_rem_q[lane] <= lane_rem_q[lane] + step_rem_q;
                  end
                end
              end else begin
                pp_q         <= pp_q + 1'b1;
                pp_acc_q     <= pp_acc_q + m_blk_i;
                row_origin_q <= origin_q;
                byte_addr_q  <= origin_q;
                ih_q         <= first_ih_q;
              end
            end else begin
              m_q <= m_q + 1'b1;
              if (ow_q + 1'b1 == w_out_i) begin
                ow_q         <= '0;
                oh_count_q   <= oh_count_q + 1'b1;
                ih_q         <= ih_q + 32'(stride_i);
                iw_q         <= -32'(pad_l_i);
                row_origin_q <= row_origin_q + row_step_q;
                byte_addr_q  <= subsample_i ? byte_addr_q + 32'(cs_i) : row_origin_q + row_step_q;
              end else begin
                ow_q        <= ow_q + 1'b1;
                iw_q        <= iw_q + 32'(stride_i);
                byte_addr_q <= byte_addr_q + (subsample_i ? 32'(cs_i) : pixel_step_q);
              end
            end
          end
        end
        FINISH:  ;
        default: state_q <= IDLE;
      endcase
    end
  end

  // Shape fields are redundant with m_blk/kb1, but catch integration mistakes.
`ifndef SYNTHESIS
  always_ff @(posedge clk) begin
    if (rst_ni && start_i && !busy_o) begin
      assert (32'(m_blk_i) == 32'(rows_i) * 32'(w_out_i));
      assert (kb1_i <= 16'(kh_i) * 16'(dw_i ? {8'b0, kw_i} : kc_n_i));
      assert ((q_i >= 1) && (q_i <= 4) && (cs_i != 0));
    end
  end
`endif
endmodule
`default_nettype wire
