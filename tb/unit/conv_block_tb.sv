`default_nettype none
// Integration fixture. Loader writes are legal only while the AGU is idle.
module conv_block_tb (
  input wire        clk,
  input wire        rst_ni,
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

  // Input/weight RAM loader and independent ACC drain controls.
  input wire        in_wr_en_i,
  input wire [12:0] in_wr_addr_i,
  input wire [63:0] in_wr_data_i,
  input wire        w_wr_en_i,
  input wire [ 9:0] w_wr_addr_i,
  input wire [63:0] w_wr_data0_i,
  input wire [63:0] w_wr_data1_i,
  input wire [ 1:0] rd_valid_i,
  input wire [ 8:0] rd_addr_i,
  input wire [ 1:0] rdata_ready_i,

  // Independently stall either array's weight acceptance in tests.
  input  wire [  1:0] weight_allow_i,
  output wire [  1:0] rd_ready_o,
  output wire [  1:0] rdata_valid_o,
  output wire [255:0] rdata0_o,
  output wire [255:0] rdata1_o,
  output wire [  1:0] acc_busy_o,
  output wire         done_o,
  output wire         busy_o,

  // Stream observation for the independent address/order scoreboard.
  output wire        a_valid_o,
  output wire [63:0] a_row0_o,
  output wire [63:0] a_row1_o,
  output wire        a_swap_o,
  output wire [ 9:0] a_tag_o,
  output wire        w_valid_o,
  output wire [63:0] w_row0_o,
  output wire [63:0] w_row1_o,
  output wire [ 1:0] w_ready_o,
  output wire [ 1:0] shadow_full_o,
  output wire [ 1:0] c_valid_o
);
  wire [  1:0] in_rd_en;
  wire [ 12:0] in_rd_addr0;
  wire [ 12:0] in_rd_addr1;
  wire [ 63:0] in_rdata0;
  wire [ 63:0] in_rdata1;
  wire         w_rd_en;
  wire [  9:0] w_rd_addr;
  wire [ 63:0] w_rdata     [2];
  wire [ 63:0] unused_w_b  [2];
  wire [255:0] c_row       [2];
  wire [  9:0] c_tag       [2];
  wire [255:0] drain_row   [2];
  wire [  1:0] joint_ready;

  assign joint_ready = w_ready_o & weight_allow_i;
  assign rdata0_o    = drain_row[0];
  assign rdata1_o    = drain_row[1];

  matrix_agu u_agu (
    .clk(clk),
    .rst_ni(rst_ni),
    .start_i(start_i),
    .dw_i(dw_i),
    .in_dense_i(in_dense_i),
    .subsample_i(subsample_i),
    .stride_i(stride_i),
    .kh_i(kh_i),
    .kw_i(kw_i),
    .pad_t_i(pad_t_i),
    .pad_l_i(pad_l_i),
    .w_in_i(w_in_i),
    .w_out_i(w_out_i),
    .cs_i(cs_i),
    .kc_n_i(kc_n_i),
    .oh0_i(oh0_i),
    .rows_i(rows_i),
    .lo_i(lo_i),
    .hi_i(hi_i),
    .m_blk_i(m_blk_i),
    .q_i(q_i),
    .pair_i(pair_i),
    .kb0_i(kb0_i),
    .kb1_i(kb1_i),
    .first_kblk_i(first_kblk_i),
    .pad_value_i(pad_value_i),
    .in_rdata0_i(in_rdata0),
    .in_rdata1_i(in_rdata1),
    .w_rdata0_i(w_rdata[0]),
    .w_rdata1_i(w_rdata[1]),
    .w_ready0_i(joint_ready[0]),
    .w_ready1_i(joint_ready[1]),
    .shadow_full0_i(shadow_full_o[0]),
    .shadow_full1_i(shadow_full_o[1]),
    .done_o(done_o),
    .busy_o(busy_o),
    .in_rd_en_o(in_rd_en),
    .in_rd_addr0_o(in_rd_addr0),
    .in_rd_addr1_o(in_rd_addr1),
    .w_rd_en_o(w_rd_en),
    .w_rd_addr_o(w_rd_addr),
    .w_valid_o(w_valid_o),
    .w_row0_o(w_row0_o),
    .w_row1_o(w_row1_o),
    .a_valid_o(a_valid_o),
    .a_row0_o(a_row0_o),
    .a_row1_o(a_row1_o),
    .a_swap_o(a_swap_o),
    .a_tag_o(a_tag_o)
  );

  ram_2p #(
    .WIDTH(64),
    .DEPTH(8192)
  ) u_input (
    .clk(clk),
    .a_en_i(in_wr_en_i || in_rd_en[0]),
    .a_we_i(in_wr_en_i),
    .a_addr_i(in_wr_en_i ? in_wr_addr_i : in_rd_addr0),
    .a_wdata_i(in_wr_data_i),
    .a_rdata_o(in_rdata0),
    .b_en_i(in_rd_en[1]),
    .b_we_i(1'b0),
    .b_addr_i(in_rd_addr1),
    .b_wdata_i(64'b0),
    .b_rdata_o(in_rdata1)
  );

  for (genvar array_idx = 0; array_idx < 2; array_idx++) begin : gen_arrays
    ram_2p #(
      .WIDTH(64),
      .DEPTH(1024)
    ) u_weight (
      .clk(clk),
      .a_en_i(w_rd_en),
      .a_we_i(1'b0),
      .a_addr_i(w_rd_addr),
      .a_wdata_i(64'b0),
      .a_rdata_o(w_rdata[array_idx]),
      .b_en_i(w_wr_en_i),
      .b_we_i(w_wr_en_i),
      .b_addr_i(w_wr_addr_i),
      .b_wdata_i(array_idx == 0 ? w_wr_data0_i : w_wr_data1_i),
      .b_rdata_o(unused_w_b[array_idx])
    );
    ws_array #(
      .TAG_W(10)
    ) u_array (
      .clk(clk),
      .rst_ni(rst_ni),
      .w_valid_i(w_valid_o),
      .w_row_i(array_idx == 0 ? w_row0_o : w_row1_o),
      .a_valid_i(a_valid_o),
      .a_row_i(array_idx == 0 ? a_row0_o : a_row1_o),
      .a_swap_i(a_swap_o),
      .a_tag_i(a_tag_o),
      .w_ready_o(w_ready_o[array_idx]),
      .shadow_full_o(shadow_full_o[array_idx]),
      .c_valid_o(c_valid_o[array_idx]),
      .c_row_o(c_row[array_idx]),
      .c_tag_o(c_tag[array_idx])
    );
    accumulator u_acc (
      .clk(clk),
      .rst_ni(rst_ni),
      .wr_valid_i(c_valid_o[array_idx]),
      .wr_bank_i(1'b0),
      .wr_addr_i(c_tag[array_idx][8:0]),
      .wr_accum_i(c_tag[array_idx][9]),
      .wr_data_i(c_row[array_idx]),
      .rd_valid_i(rd_valid_i[array_idx]),
      .rd_bank_i(1'b0),
      .rd_addr_i(rd_addr_i),
      .rdata_ready_i(rdata_ready_i[array_idx]),
      .rd_ready_o(rd_ready_o[array_idx]),
      .rdata_valid_o(rdata_valid_o[array_idx]),
      .rdata_o(drain_row[array_idx]),
      .busy_o(acc_busy_o[array_idx])
    );
  end
endmodule
`default_nettype wire
