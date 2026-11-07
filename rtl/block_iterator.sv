`default_nettype none
// ROLE: 0 input, 1 weights/coefficients, 2 compute, 3 drain. All roles walk
// the same logical loop independently. Setup products use a shared shift/add
// engine; there are no combinational multipliers or dividers on the issue path.
// plan_i packs plan_unit outputs from LSB in declaration order; bits 255:229
// are reserved. desc_o is 21 little-endian 32-bit words:
//   oh0, rows, lo, hi, M, q, pair/nb, kb0, kb1, ch0, nch, offset,
//   ng, npix, input address, input beats, output address, output beats,
//   flags, weight address, coefficient address.
// flags[3:0] = {last_n, last_m, last_k, first_k}. ADD offset is bytes;
// AVG offset is channel groups. Descriptor data holds until ready/valid fires.
module block_iterator #(
  parameter int unsigned ROLE = 2
) (
  input  wire          clk,
  input  wire          rst_ni,
  input  wire          start_i,
  input  wire  [511:0] instr_i,
  input  wire  [255:0] plan_i,
  input  wire          desc_ready_i,
  output wire          desc_valid_o,
  output logic [671:0] desc_o,
  output logic         done_o
);
  wire [ 3:0] layer_type = instr_i[60+:4];
  wire [ 3:0] kh = instr_i[56+:4];
  wire [ 3:0] kw = instr_i[52+:4];
  wire [ 3:0] stride = instr_i[48+:4];
  wire [ 3:0] pad_t = instr_i[44+:4];
  wire [ 3:0] pad_l = instr_i[40+:4];
  wire [ 3:0] pad_b = instr_i[36+:4];
  wire [ 3:0] pad_r = instr_i[32+:4];
  wire [ 0:0] in_dense = instr_i[31+:1];
  wire [11:0] h_in = instr_i[116+:12];
  wire [11:0] w_in = instr_i[104+:12];
  wire [11:0] h_out = instr_i[92+:12];
  wire [11:0] w_out = instr_i[80+:12];
  wire [11:0] c_in = instr_i[180+:12];
  wire [11:0] c_out = instr_i[168+:12];
  wire [ 7:0] zp_in = instr_i[160+:8];
  wire [ 7:0] zp_in2 = instr_i[152+:8];
  wire [ 7:0] zp_out = instr_i[144+:8];
  wire [ 7:0] act_min = instr_i[136+:8];
  wire [ 7:0] act_max = instr_i[128+:8];
  wire [ 7:0] pad_value = instr_i[248+:8];
  wire [31:0] scalar0 = instr_i[288+:32];
  wire [31:0] scalar1 = instr_i[256+:32];
  wire [31:0] in_base = instr_i[352+:32];
  wire [31:0] in2_base = instr_i[320+:32];
  wire [31:0] coef_base = instr_i[416+:32];
  wire [31:0] out_base = instr_i[384+:32];
  wire        unused_instruction = ^instr_i;
  wire [15:0] plan_cs = plan_i[0+:16];
  wire [15:0] plan_kc_n = plan_i[16+:16];
  wire [23:0] plan_tiles_k = plan_i[32+:24];
  wire [15:0] plan_n_tiles = plan_i[56+:16];
  wire [15:0] plan_n_pairs = plan_i[72+:16];
  wire [ 2:0] plan_pairs_per_blk = plan_i[88+:3];
  wire [15:0] plan_n_nblk = plan_i[91+:16];
  wire [15:0] plan_tiles_per_kblk = plan_i[107+:16];
  wire [23:0] plan_k_blks = plan_i[123+:24];
  wire [15:0] plan_rows_per_blk = plan_i[147+:16];
  wire [23:0] plan_m_blk = plan_i[163+:24];
  wire [23:0] plan_n_mblk = plan_i[187+:24];
  wire [ 0:0] plan_n_outer = plan_i[211+:1];
  wire [ 0:0] plan_subsample = plan_i[212+:1];
  wire [15:0] plan_avg_groups = plan_i[213+:16];
  wire        unused_plan = ^plan_i;

  typedef enum logic [4:0] {
    IDLE,
    ROW_PITCH,
    PIXELS,
    TOTAL_BYTES,
    PREPARE,
    GEOMETRY,
    INPUT_ORIGIN,
    BOUNDS,
    CLIP,
    OUTPUT_PIXEL,
    OUTPUT_ORIGIN,
    WEIGHT_TILE,
    WEIGHT_ORIGIN,
    LENGTH,
    PRESENT,
    ADVANCE,
    MULTIPLY
  } state_t;
  state_t state_q, return_q;
  logic [31:0] mul_lhs_q, mul_rhs_q, mul_sum_q;
  logic [31:0] row_bytes_q, pixels_q, total_bytes_q;
  logic [31:0] oh0_q, nb_q, kb0_q, offset_q;
  logic [31:0] rows_q, lo_q, hi_q, m_q, q_q, kb1_q, ch0_q, nch_q, ng_q;
  logic [31:0] input_beats_q, output_beats_q;
  logic [31:0] in_origin_q, out_origin_q, weight_origin_q;
  wire [31:0] nblocks = (layer_type == 1) ? 32'(plan_n_pairs) : 32'(plan_n_nblk);
  wire last_m = oh0_q + rows_q >= 32'(h_out);
  wire last_n = nb_q + 1 >= nblocks;
  wire last_k = kb1_q >= 32'(plan_tiles_k);
  wire [31:0] clipped_rows = ((32'(h_out) - oh0_q) < 32'(plan_rows_per_blk)) ?
      32'(h_out) - oh0_q : 32'(plan_rows_per_blk);
  logic [31:0] first_input_q, end_output_q, end_input_q, high_last_q;
  wire [31:0] padded_out = (32'(c_out) + 7) & 32'hffff_fff8;
  wire [31:0] pair_base = ((plan_pairs_per_blk == 4) ? nb_q << 2 :
      ((plan_pairs_per_blk == 3) ? (nb_q << 1) + nb_q :
       ((plan_pairs_per_blk == 2) ? nb_q << 1 : nb_q)));
  wire unused_fields = ^{kh, kw, pad_l, pad_b, pad_r, in_dense, c_in, zp_in, zp_in2,
      zp_out, act_min, act_max, pad_value, scalar0, scalar1, in2_base,
      plan_kc_n, plan_n_tiles, plan_k_blks, plan_m_blk, plan_n_mblk};

  task automatic multiply(input logic [31:0] lhs, input logic [31:0] rhs,
                          input state_t continuation);
    mul_lhs_q <= lhs;
    mul_rhs_q <= rhs;
    mul_sum_q <= '0;
    return_q  <= continuation;
    state_q   <= MULTIPLY;
  endtask
  assign desc_valid_o = (state_q == PRESENT) && (input_beats_q != 0);
  always_comb begin
    desc_o          = '0;
    desc_o[0+:32]   = oh0_q;
    desc_o[32+:32]  = rows_q;
    desc_o[64+:32]  = lo_q;
    desc_o[96+:32]  = hi_q;
    desc_o[128+:32] = m_q;
    desc_o[160+:32] = q_q;
    desc_o[192+:32] = nb_q;
    desc_o[224+:32] = kb0_q;
    desc_o[256+:32] = kb1_q;
    desc_o[288+:32] = ch0_q;
    desc_o[320+:32] = nch_q;
    desc_o[352+:32] = offset_q;
    desc_o[384+:32] = ng_q;
    desc_o[416+:32] = pixels_q;
    desc_o[448+:32] = in_origin_q;
    desc_o[480+:32] = input_beats_q;
    desc_o[512+:32] = out_origin_q;
    desc_o[544+:32] = output_beats_q;
    desc_o[576+:32] = {28'b0, last_n, last_m, last_k, (kb0_q == 0)};
    desc_o[608+:32] = weight_origin_q;
    desc_o[640+:32] = coef_base + (ch0_q << 3);
  end
  // Each accepted descriptor advances only this role's resource lifetime.
  always_ff @(posedge clk) begin
    if (!rst_ni) begin
      state_q <= IDLE;
      done_o  <= 1'b0;
    end else begin
      done_o <= 1'b0;
      case (state_q)
        IDLE:
        if (start_i) begin
          oh0_q    <= 0;
          nb_q     <= 0;
          kb0_q    <= 0;
          offset_q <= 0;
          multiply(32'(w_in), 32'(plan_cs), ROW_PITCH);
        end
        ROW_PITCH: begin
          row_bytes_q <= mul_sum_q;
          multiply(32'(h_in), 32'(w_in), PIXELS);
        end
        PIXELS: begin
          pixels_q <= mul_sum_q;
          multiply(mul_sum_q, 32'(plan_cs), TOTAL_BYTES);
        end
        TOTAL_BYTES: begin
          total_bytes_q <= mul_sum_q;
          state_q       <= PREPARE;
        end
        PREPARE: begin
          rows_q <= clipped_rows;
          q_q <= (layer_type == 0) ?
              ((32'(plan_n_pairs) - pair_base < 32'(plan_pairs_per_blk)) ?
               32'(plan_n_pairs) - pair_base : 32'(plan_pairs_per_blk)) : 1;
          ch0_q <= (layer_type == 0) ? pair_base << 4 : nb_q << 4;
          ng_q <= (32'(plan_cs >> 3) - offset_q < 32'(plan_avg_groups)) ?
              32'(plan_cs >> 3) - offset_q : 32'(plan_avg_groups);
          kb1_q <= ((layer_type != 0) ||
              (kb0_q + 32'(plan_tiles_per_kblk) > 32'(plan_tiles_k))) ?
              32'(plan_tiles_k) : kb0_q + 32'(plan_tiles_per_kblk);
          state_q <= GEOMETRY;
        end
        GEOMETRY: begin
          first_input_q <= oh0_q << (stride == 2 ? 1 : 0);
          end_output_q <= oh0_q + rows_q - 1;
          nch_q <= (padded_out - ch0_q < (q_q << 4)) ? padded_out - ch0_q : q_q << 4;
          multiply(rows_q, 32'(w_out), INPUT_ORIGIN);
        end
        INPUT_ORIGIN: begin
          m_q <= mul_sum_q;
          lo_q <= first_input_q < 32'(pad_t) ? 0 : first_input_q - 32'(pad_t);
          end_input_q <= (end_output_q << (stride == 2 ? 1 : 0)) + 32'(kh);
          state_q <= BOUNDS;
        end
        BOUNDS: begin
          high_last_q <= end_input_q - 32'(pad_t) - 1;
          state_q <= CLIP;
        end
        CLIP: begin
          hi_q <= high_last_q >= 32'(h_in) ? 32'(h_in) - 1 : high_last_q;
          multiply(lo_q, row_bytes_q, OUTPUT_PIXEL);
        end
        OUTPUT_PIXEL: begin
          in_origin_q <= in_base + mul_sum_q;
          if (layer_type == 2) in_origin_q <= in_base + offset_q;
          if (layer_type == 4) in_origin_q <= in_base + (offset_q << 3);
          multiply(oh0_q, 32'(w_out), OUTPUT_ORIGIN);
        end
        OUTPUT_ORIGIN: begin
          multiply(mul_sum_q, padded_out, WEIGHT_TILE);
        end
        WEIGHT_TILE: begin
          out_origin_q <= out_base + mul_sum_q + ch0_q;
          if (layer_type == 2) out_origin_q <= out_base + offset_q;
          if (layer_type == 4) out_origin_q <= out_base + (offset_q << 3);
          multiply((layer_type == 0) ? ch0_q >> 3 : ch0_q, 32'(plan_tiles_k), WEIGHT_ORIGIN);
        end
        WEIGHT_ORIGIN: begin
          weight_origin_q <= in2_base + ((layer_type == 0) ?
              ((mul_sum_q + kb0_q) << 6) : mul_sum_q);
          // Use the same multiplier for bank lengths, after addresses are latched.
          multiply(
              (layer_type == 2) ? 1 :
              ((layer_type == 4) ? 1 : (plan_subsample ? m_q : hi_q - lo_q + 1)),
              (layer_type == 2) ? ((total_bytes_q - offset_q < 16384) ?
               total_bytes_q - offset_q : 32'd16384) :
              ((layer_type == 4) ? ng_q << 3 : (plan_subsample ? 32'(plan_cs) : row_bytes_q)),
              LENGTH);
          input_beats_q <= 0;
        end
        LENGTH: begin
          input_beats_q <= mul_sum_q >> 3;
          multiply(m_q, 32'(plan_cs >> 3), PRESENT);
        end
        PRESENT: if (desc_ready_i) state_q <= ADVANCE;
        ADVANCE: begin
          state_q <= PREPARE;
          if (layer_type == 2) begin
            offset_q <= offset_q + 16384;
            if (offset_q + 16384 >= total_bytes_q) begin
              state_q <= IDLE;
              done_o  <= 1'b1;
            end
          end else if (layer_type == 4) begin
            offset_q <= offset_q + ng_q;
            if (offset_q + ng_q >= 32'(plan_cs >> 3)) begin
              state_q <= IDLE;
              done_o  <= 1'b1;
            end
          end else if ((layer_type == 0) && ((ROLE == 2) || ((ROLE == 1) && !plan_n_outer)) &&
          !last_k) begin
            kb0_q <= kb1_q;
          end else begin
            kb0_q <= 0;
            if ((ROLE == 0 && !plan_n_outer) || (layer_type == 3)) begin
              oh0_q <= oh0_q + rows_q;
              if (last_m) begin
                state_q <= IDLE;
                done_o  <= 1'b1;
              end
            end else if (ROLE == 1 && plan_n_outer) begin
              nb_q <= nb_q + 1;
              if (last_n) begin
                state_q <= IDLE;
                done_o  <= 1'b1;
              end
            end else if (plan_n_outer) begin
              if (last_m) begin
                oh0_q <= 0;
                nb_q  <= nb_q + 1;
                if (last_n) begin
                  state_q <= IDLE;
                  done_o  <= 1'b1;
                end
              end else oh0_q <= oh0_q + rows_q;
            end else begin
              if (last_n) begin
                nb_q  <= 0;
                oh0_q <= oh0_q + rows_q;
                if (last_m) begin
                  state_q <= IDLE;
                  done_o  <= 1'b1;
                end
              end else nb_q <= nb_q + 1;
            end
          end
        end
        MULTIPLY: begin
          if (mul_rhs_q == 0) state_q <= return_q;
          else begin
            if (mul_rhs_q[0]) mul_sum_q <= mul_sum_q + mul_lhs_q;
            mul_lhs_q <= mul_lhs_q << 1;
            mul_rhs_q <= mul_rhs_q >> 1;
          end
        end
        default: state_q <= IDLE;
      endcase
    end
  end
  // MAX output length is the staged M*groups product; ADD/AVG use chunk lengths.
  // CONV/DW drain issues nch/8 per pixel instead of one contiguous block.
  always_comb begin
    if (layer_type == 2) output_beats_q = input_beats_q;
    else if (layer_type == 4) output_beats_q = ng_q;
    else if (layer_type == 3) output_beats_q = mul_sum_q;
    else output_beats_q = nch_q >> 3;
  end
endmodule
`default_nettype wire
