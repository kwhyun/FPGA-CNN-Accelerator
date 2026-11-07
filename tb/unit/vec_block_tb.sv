`default_nettype none
module vec_block_tb (
  // Clock, configuration, RAM loading, and observed streams.
  input  wire        clk,
  input  wire        rst_ni,
  input  wire        start_i,
  input  wire [ 1:0] op_i,
  input  wire [ 3:0] stride_i,
  input  wire [ 3:0] kh_i,
  input  wire [ 3:0] kw_i,
  input  wire [ 3:0] pad_t_i,
  input  wire [ 3:0] pad_l_i,
  input  wire [11:0] h_in_i,
  input  wire [11:0] w_in_i,
  input  wire [11:0] w_out_i,
  input  wire [11:0] lo_i,
  input  wire [11:0] oh0_i,
  input  wire [11:0] rows_i,
  input  wire [ 9:0] groups_i,
  input  wire [15:0] npix_i,
  input  wire [15:0] nrows_i,
  input  wire [ 7:0] pad_value_i,
  input  wire [ 7:0] zp_a_i,
  input  wire [ 7:0] zp_b_i,
  input  wire [ 7:0] zp_out_i,
  input  wire [ 7:0] act_min_i,
  input  wire [ 7:0] act_max_i,
  input  wire [31:0] scalar0_i,
  input  wire [31:0] scalar1_i,
  input  wire [12:0] in_wr_addr_i,
  input  wire [63:0] in_wr_data_i,
  input  wire [10:0] w_wr_addr_i,
  input  wire [63:0] w_wr_data_i,
  input  wire        in_wr_en_i,
  input  wire        w_wr_en_i,
  input  wire        out_ready_i,
  output wire        done_o,
  output wire        busy_o,
  output wire        out_valid_o,
  output wire [63:0] out_data_o,
  output wire        v_valid_o,
  output wire        v_ready_o,
  output wire [63:0] v_a_o,
  output wire [63:0] v_b_o,
  output wire        v_first_o,
  output wire        v_last_o,
  output wire [ 1:0] in_rd_en_o,
  output wire [12:0] in_rd_addr0_o,
  output wire [12:0] in_rd_addr1_o,
  output wire        w_rd_en_o,
  output wire [10:0] w_rd_addr_o
);
  wire [63:0] in_rdata0;
  wire [63:0] in_rdata1;
  wire [63:0] w_rdata0;
  wire [63:0] w_rdata1;
  wire [63:0] unused_w_write_read_data0;
  wire [63:0] unused_w_write_read_data1;
  vector_agu u_vector_agu (
    .clk             (clk),
    .rst_ni          (rst_ni),
    .start_i         (start_i),
    .op_i            (op_i),
    .stride_i        (stride_i),
    .kh_i            (kh_i),
    .kw_i            (kw_i),
    .pad_t_i         (pad_t_i),
    .pad_l_i         (pad_l_i),
    .h_in_i          (h_in_i),
    .w_in_i          (w_in_i),
    .w_out_i         (w_out_i),
    .lo_i            (lo_i),
    .oh0_i           (oh0_i),
    .rows_i          (rows_i),
    .groups_i        (groups_i),
    .npix_i          (npix_i),
    .nrows_i         (nrows_i),
    .pad_value_i     (pad_value_i),
    .in_rdata0_i     (in_rdata0),
    .in_rdata1_i     (in_rdata1),
    .w_rdata0_i      (w_rdata0),
    .w_rdata1_i      (w_rdata1),
    .v_ready_i       (v_ready_o),
    .done_o          (done_o),
    .busy_o          (busy_o),
    .in_rd_en_o      (in_rd_en_o),
    .in_rd_addr0_o   (in_rd_addr0_o),
    .in_rd_addr1_o   (in_rd_addr1_o),
    .w_rd_en_o       (w_rd_en_o),
    .w_rd_addr_o     (w_rd_addr_o),
    .v_valid_o       (v_valid_o),
    .v_a_o           (v_a_o),
    .v_b_o           (v_b_o),
    .v_first_o       (v_first_o),
    .v_last_o        (v_last_o)
  );
  vector_unit u_vector_unit (
    .clk             (clk),
    .rst_ni          (rst_ni),
    .op_i            (op_i),
    .zp_a_i          (zp_a_i),
    .zp_b_i          (zp_b_i),
    .zp_out_i        (zp_out_i),
    .act_min_i       (act_min_i),
    .act_max_i       (act_max_i),
    .scalar0_i       (scalar0_i),
    .scalar1_i       (scalar1_i),
    .out_ready_i     (out_ready_i),
    .out_valid_o     (out_valid_o),
    .out_data_o      (out_data_o),
    .in_valid_i      (v_valid_o),
    .in_a_i          (v_a_o),
    .in_b_i          (v_b_o),
    .in_first_i      (v_first_o),
    .in_last_i       (v_last_o),
    .in_ready_o      (v_ready_o)
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(8192)
  ) u_input_ram (
    .clk             (clk),
    .a_en_i          (in_rd_en_o[0]),
    .a_we_i          (1'b0),
    .a_addr_i        (in_rd_addr0_o),
    .a_wdata_i       (64'd0),
    .a_rdata_o       (in_rdata0),
    .b_en_i          (in_wr_en_i || in_rd_en_o[1]),
    .b_we_i          (in_wr_en_i),
    .b_addr_i        (in_wr_en_i ? in_wr_addr_i : in_rd_addr1_o),
    .b_wdata_i       (in_wr_data_i),
    .b_rdata_o       (in_rdata1)
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(1024)
  ) u_weight_ram0 (
    .clk             (clk),
    .a_en_i          (w_rd_en_o && (w_rd_addr_o[10] == 1'b0)),
    .a_we_i          (1'b0),
    .a_addr_i        (w_rd_addr_o[9:0]),
    .a_wdata_i       (64'd0),
    .a_rdata_o       (w_rdata0),
    .b_en_i          (w_wr_en_i && (w_wr_addr_i[10] == 1'b0)),
    .b_we_i          (1'b1),
    .b_addr_i        (w_wr_addr_i[9:0]),
    .b_wdata_i       (w_wr_data_i),
    .b_rdata_o       (unused_w_write_read_data0)
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(1024)
  ) u_weight_ram1 (
    .clk             (clk),
    .a_en_i          (w_rd_en_o && (w_rd_addr_o[10] == 1'b1)),
    .a_we_i          (1'b0),
    .a_addr_i        (w_rd_addr_o[9:0]),
    .a_wdata_i       (64'd0),
    .a_rdata_o       (w_rdata1),
    .b_en_i          (w_wr_en_i && (w_wr_addr_i[10] == 1'b1)),
    .b_we_i          (1'b1),
    .b_addr_i        (w_wr_addr_i[9:0]),
    .b_wdata_i       (w_wr_data_i),
    .b_rdata_o       (unused_w_write_read_data1)
  );
endmodule
`default_nettype wire
