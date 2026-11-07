`default_nettype none
// Two-array INT8 accelerator. Independent resource iterators overlap input DMA,
// weight DMA, compute and drain; AXI responses are fenced between layers.
module accel_top (
  input  wire        clk,
  input  wire        rst_ni,
  input  wire [11:0] s_axil_awaddr,
  input  wire [ 2:0] s_axil_awprot,
  input  wire        s_axil_awvalid,
  input  wire [31:0] s_axil_wdata,
  input  wire [ 3:0] s_axil_wstrb,
  input  wire        s_axil_wvalid,
  input  wire        s_axil_bready,
  input  wire [11:0] s_axil_araddr,
  input  wire [ 2:0] s_axil_arprot,
  input  wire        s_axil_arvalid,
  input  wire        s_axil_rready,
  output wire        s_axil_awready,
  output wire        s_axil_wready,
  output wire [ 1:0] s_axil_bresp,
  output wire        s_axil_bvalid,
  output wire        s_axil_arready,
  output wire [31:0] s_axil_rdata,
  output wire [ 1:0] s_axil_rresp,
  output wire        s_axil_rvalid,
  input  wire        m_axi_in_arready,
  input  wire [ 0:0] m_axi_in_rid,
  input  wire [63:0] m_axi_in_rdata,
  input  wire [ 1:0] m_axi_in_rresp,
  input  wire        m_axi_in_rlast,
  input  wire        m_axi_in_rvalid,
  output wire [ 0:0] m_axi_in_arid,
  output wire [31:0] m_axi_in_araddr,
  output wire [ 7:0] m_axi_in_arlen,
  output wire [ 2:0] m_axi_in_arsize,
  output wire [ 1:0] m_axi_in_arburst,
  output wire        m_axi_in_arlock,
  output wire [ 3:0] m_axi_in_arcache,
  output wire [ 2:0] m_axi_in_arprot,
  output wire [ 3:0] m_axi_in_arqos,
  output wire [ 3:0] m_axi_in_arregion,
  output wire        m_axi_in_arvalid,
  output wire        m_axi_in_rready,
  input  wire        m_axi_w_arready,
  input  wire [ 0:0] m_axi_w_rid,
  input  wire [63:0] m_axi_w_rdata,
  input  wire [ 1:0] m_axi_w_rresp,
  input  wire        m_axi_w_rlast,
  input  wire        m_axi_w_rvalid,
  output wire [ 0:0] m_axi_w_arid,
  output wire [31:0] m_axi_w_araddr,
  output wire [ 7:0] m_axi_w_arlen,
  output wire [ 2:0] m_axi_w_arsize,
  output wire [ 1:0] m_axi_w_arburst,
  output wire        m_axi_w_arlock,
  output wire [ 3:0] m_axi_w_arcache,
  output wire [ 2:0] m_axi_w_arprot,
  output wire [ 3:0] m_axi_w_arqos,
  output wire [ 3:0] m_axi_w_arregion,
  output wire        m_axi_w_arvalid,
  output wire        m_axi_w_rready,
  input  wire        m_axi_out_awready,
  input  wire        m_axi_out_wready,
  input  wire [ 0:0] m_axi_out_bid,
  input  wire [ 1:0] m_axi_out_bresp,
  input  wire        m_axi_out_bvalid,
  output wire [ 0:0] m_axi_out_awid,
  output wire [31:0] m_axi_out_awaddr,
  output wire [ 7:0] m_axi_out_awlen,
  output wire [ 2:0] m_axi_out_awsize,
  output wire [ 1:0] m_axi_out_awburst,
  output wire        m_axi_out_awlock,
  output wire [ 3:0] m_axi_out_awcache,
  output wire [ 2:0] m_axi_out_awprot,
  output wire [ 3:0] m_axi_out_awqos,
  output wire [ 3:0] m_axi_out_awregion,
  output wire        m_axi_out_awvalid,
  output wire [63:0] m_axi_out_wdata,
  output wire [ 7:0] m_axi_out_wstrb,
  output wire        m_axi_out_wlast,
  output wire        m_axi_out_wvalid,
  output wire        m_axi_out_bready,
  output wire        irq_o
);
  wire         core_rst_n;
  wire         start;
  wire         soft_reset;
  wire [ 31:0] instr_base;
  wire [ 31:0] instr_count;
  wire [511:0] instr;
  wire [255:0] plan_bus;
  wire         block_start;
  wire         block_done;
  wire         engine_reset;
  wire         engines_idle;
  wire         axi_error;
  wire         layer_busy;
  wire         layer_done;
  wire         layer_error;
  wire [ 31:0] layer_index;
  wire [ 31:0] error_code;
  wire [  6:0] perf_events;
  wire         layer_cmd_ready;
  wire         layer_data_valid;
  wire [ 63:0] layer_data;
  wire         layer_plan_done;
  wire         layer_plan_error;
  wire         layer_cmd_valid;
  wire [ 31:0] layer_cmd_addr;
  wire         layer_data_ready;
  wire         layer_plan_start;
  wire         iterator0_desc_ready;
  wire         iterator0_desc_valid;
  wire [671:0] iterator0_desc;
  wire         iterator0_done;
  wire         iterator1_desc_ready;
  wire         iterator1_desc_valid;
  wire [671:0] iterator1_desc;
  wire         iterator1_done;
  wire         iterator2_desc_ready;
  wire         iterator2_desc_valid;
  wire [671:0] iterator2_desc;
  wire         iterator2_done;
  wire         iterator3_desc_ready;
  wire         iterator3_desc_valid;
  wire [671:0] iterator3_desc;
  wire         iterator3_done;
  wire         input_pingpong_publish;
  wire         input_pingpong_release;
  wire         input_pingpong_produce_ready;
  wire         input_pingpong_consume_valid;
  wire         input_pingpong_produce_bank;
  wire         input_pingpong_consume_bank;
  wire         weight_pingpong_publish;
  wire         weight_pingpong_release;
  wire         weight_pingpong_produce_ready;
  wire         weight_pingpong_consume_valid;
  wire         weight_pingpong_produce_bank;
  wire         weight_pingpong_consume_bank;
  wire         coefficient_pingpong_publish;
  wire         coefficient_pingpong_release;
  wire         coefficient_pingpong_produce_ready;
  wire         coefficient_pingpong_consume_valid;
  wire         coefficient_pingpong_produce_bank;
  wire         coefficient_pingpong_consume_bank;
  wire         accumulator_pingpong_publish;
  wire         accumulator_pingpong_release;
  wire         accumulator_pingpong_produce_ready;
  wire         accumulator_pingpong_consume_valid;
  wire         accumulator_pingpong_produce_bank;
  wire         accumulator_pingpong_consume_bank;
  wire         input_loader_cmd_ready;
  wire         input_loader_data_valid;
  wire [ 63:0] input_loader_data;
  wire         input_loader_data_last;
  wire         input_loader_cmd_valid;
  wire [ 31:0] input_loader_cmd_addr;
  wire [ 19:0] input_loader_cmd_beats;
  wire         input_loader_data_ready;
  wire         input_loader_wr_en;
  wire         input_loader_wr_bank;
  wire [ 12:0] input_loader_wr_addr;
  wire [ 63:0] input_loader_wr_data;
  wire         input_loader_idle;
  wire         weight_loader_cmd_ready;
  wire         weight_loader_data_valid;
  wire [ 63:0] weight_loader_data;
  wire         weight_loader_data_last;
  wire         weight_loader_cmd_valid;
  wire [ 31:0] weight_loader_cmd_addr;
  wire [ 19:0] weight_loader_cmd_beats;
  wire         weight_loader_data_ready;
  wire         weight_loader_wr_en;
  wire         weight_loader_wr_bank;
  wire         weight_loader_wr_array;
  wire [  9:0] weight_loader_wr_addr;
  wire [ 63:0] weight_loader_wr_data;
  wire         weight_loader_coef_wr_en;
  wire         weight_loader_coef_wr_bank;
  wire [  5:0] weight_loader_coef_wr_addr;
  wire         weight_loader_idle;
  wire         compute_vector_allow;
  wire         compute_agu_done;
  wire [  1:0] compute_acc_busy;
  wire [671:0] compute_active_desc;
  wire         compute_in_bank;
  wire         compute_w_bank;
  wire         compute_acc_bank;
  wire         compute_conv_start;
  wire         compute_vec_start;
  wire         compute_idle;
  wire [ 63:0] drain_coef_rdata0;
  wire [ 63:0] drain_coef_rdata1;
  wire [  1:0] drain_acc_ready;
  wire [  1:0] drain_acc_rvalid;
  wire [255:0] drain_acc_rdata0;
  wire [255:0] drain_acc_rdata1;
  wire         drain_vector_valid;
  wire [ 63:0] drain_vector_data;
  wire         drain_cmd_ready;
  wire         drain_data_ready;
  wire         drain_coef_rd_en;
  wire         drain_coef_bank;
  wire [  5:0] drain_coef_addr0;
  wire [  5:0] drain_coef_addr1;
  wire [  1:0] drain_acc_rd_valid;
  wire         drain_acc_bank;
  wire [  8:0] drain_acc_addr;
  wire [  1:0] drain_acc_rready;
  wire         drain_vector_ready;
  wire         drain_cmd_valid;
  wire [ 31:0] drain_cmd_addr;
  wire [ 19:0] drain_cmd_beats;
  wire         drain_data_valid;
  wire [ 63:0] drain_data;
  wire         drain_idle;
  wire         input_dma_cmd_valid;
  wire [ 31:0] input_dma_cmd_addr;
  wire [ 19:0] input_dma_cmd_beats;
  wire         input_dma_data_ready;
  wire         input_dma_cmd_ready;
  wire         input_dma_data_valid;
  wire [ 63:0] input_dma_data;
  wire         input_dma_data_last;
  wire         input_dma_error;
  wire         weight_dma_error;
  wire         output_dma_idle;
  wire         output_dma_error;
  wire         conv_done;
  wire         vec_done;
  wire [  1:0] acc_busy;
  wire [ 63:0] input_rdata0;
  wire [ 63:0] input_rdata1;
  wire [ 63:0] weight_rdata0;
  wire [ 63:0] weight_rdata1;
  wire [  1:0] array_ready;
  wire [  1:0] array_shadow;
  wire         matrix_agu_busy;
  wire [  1:0] matrix_agu_in_rd_en;
  wire [ 12:0] matrix_agu_in_rd_addr0;
  wire [ 12:0] matrix_agu_in_rd_addr1;
  wire         matrix_agu_w_rd_en;
  wire [  9:0] matrix_agu_w_rd_addr;
  wire         matrix_agu_w_valid;
  wire [ 63:0] matrix_agu_w_row0;
  wire [ 63:0] matrix_agu_w_row1;
  wire         matrix_agu_a_valid;
  wire [ 63:0] matrix_agu_a_row0;
  wire [ 63:0] matrix_agu_a_row1;
  wire         matrix_agu_a_swap;
  wire [  9:0] matrix_agu_a_tag;
  wire [  1:0] vector_op;
  wire         vector_agu_v_ready;
  wire [  1:0] vector_agu_in_rd_en;
  wire [ 12:0] vector_agu_in_rd_addr0;
  wire [ 12:0] vector_agu_in_rd_addr1;
  wire         vector_agu_w_rd_en;
  wire [ 10:0] vector_agu_w_rd_addr;
  wire         vector_agu_v_valid;
  wire [ 63:0] vector_agu_v_a;
  wire [ 63:0] vector_agu_v_b;
  wire         vector_agu_v_first;
  wire         vector_agu_v_last;
  wire [  1:0] input_rd_en;
  wire [ 12:0] input_addr0;
  wire [ 12:0] input_addr1;
  wire [  1:0] weight_rd_en;
  wire [  9:0] weight_addr;
  wire         array0_valid;
  wire [255:0] array0_row;
  wire [ 10:0] array0_tag;
  wire         array1_valid;
  wire [255:0] array1_row;
  wire [ 10:0] array1_tag;
  wire         unused_vector_agu_busy;
  wire [ 63:0] unused_unused_weight0_0;
  wire [ 63:0] unused_unused_weight0_1;
  wire [ 63:0] unused_unused_weight1_0;
  wire [ 63:0] unused_unused_weight1_1;
  control_status u_control (
    .clk(clk),
    .rst_ni(rst_ni),
    .s_axil_awaddr(s_axil_awaddr),
    .s_axil_awprot(s_axil_awprot),
    .s_axil_awvalid(s_axil_awvalid),
    .s_axil_wdata(s_axil_wdata),
    .s_axil_wstrb(s_axil_wstrb),
    .s_axil_wvalid(s_axil_wvalid),
    .s_axil_bready(s_axil_bready),
    .s_axil_araddr(s_axil_araddr),
    .s_axil_arprot(s_axil_arprot),
    .s_axil_arvalid(s_axil_arvalid),
    .s_axil_rready(s_axil_rready),
    .busy_i(layer_busy),
    .done_i(layer_done),
    .error_i(layer_error),
    .layer_index_i(layer_index),
    .error_code_i(error_code),
    .perf_events_i(perf_events),
    .s_axil_awready(s_axil_awready),
    .s_axil_wready(s_axil_wready),
    .s_axil_bresp(s_axil_bresp),
    .s_axil_bvalid(s_axil_bvalid),
    .s_axil_arready(s_axil_arready),
    .s_axil_rdata(s_axil_rdata),
    .s_axil_rresp(s_axil_rresp),
    .s_axil_rvalid(s_axil_rvalid),
    .start_o(start),
    .soft_reset_o(soft_reset),
    .instr_base_o(instr_base),
    .instr_count_o(instr_count),
    .irq_o(irq_o)
  );
  layer_control u_layer (
    .clk(clk),
    .rst_ni(rst_ni),
    .start_i(start),
    .soft_reset_i(soft_reset),
    .instr_base_i(instr_base),
    .instr_count_i(instr_count),
    .cmd_ready_i(layer_cmd_ready),
    .data_valid_i(layer_data_valid),
    .data_i(layer_data),
    .plan_done_i(layer_plan_done),
    .plan_error_i(layer_plan_error),
    .block_done_i(block_done),
    .engines_idle_i(engines_idle),
    .axi_error_i(axi_error),
    .cmd_valid_o(layer_cmd_valid),
    .cmd_addr_o(layer_cmd_addr),
    .data_ready_o(layer_data_ready),
    .instr_o(instr),
    .plan_start_o(layer_plan_start),
    .block_start_o(block_start),
    .reset_engines_o(engine_reset),
    .busy_o(layer_busy),
    .done_o(layer_done),
    .error_o(layer_error),
    .layer_index_o(layer_index),
    .error_code_o(error_code)
  );
  plan_unit u_plan (
    .clk(clk),
    .rst_ni(core_rst_n),
    .start_i(layer_plan_start),
    .layer_type_i(instr[60 +: 4]),
    .kh_i(instr[56 +: 4]),
    .kw_i(instr[52 +: 4]),
    .stride_i(instr[48 +: 4]),
    .pad_t_i(instr[44 +: 4]),
    .pad_l_i(instr[40 +: 4]),
    .pad_b_i(instr[36 +: 4]),
    .pad_r_i(instr[32 +: 4]),
    .in_dense_i(instr[31 +: 1]),
    .h_in_i(instr[116 +: 12]),
    .w_in_i(instr[104 +: 12]),
    .h_out_i(instr[92 +: 12]),
    .w_out_i(instr[80 +: 12]),
    .c_in_i(instr[180 +: 12]),
    .c_out_i(instr[168 +: 12]),
    .done_o(layer_plan_done),
    .error_o(layer_plan_error),
    .cs_o(plan_bus[0 +: 16]),
    .kc_n_o(plan_bus[16 +: 16]),
    .tiles_k_o(plan_bus[32 +: 24]),
    .n_tiles_o(plan_bus[56 +: 16]),
    .n_pairs_o(plan_bus[72 +: 16]),
    .pairs_per_blk_o(plan_bus[88 +: 3]),
    .n_nblk_o(plan_bus[91 +: 16]),
    .tiles_per_kblk_o(plan_bus[107 +: 16]),
    .k_blks_o(plan_bus[123 +: 24]),
    .rows_per_blk_o(plan_bus[147 +: 16]),
    .m_blk_o(plan_bus[163 +: 24]),
    .n_mblk_o(plan_bus[187 +: 24]),
    .n_outer_o(plan_bus[211 +: 1]),
    .subsample_o(plan_bus[212 +: 1]),
    .avg_groups_o(plan_bus[213 +: 16])
  );
  assign plan_bus[255:229] = '0;
  assign core_rst_n        = rst_ni && !engine_reset;
  block_iterator #(
    .ROLE(0)
  ) u_iterator0 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .start_i(block_start),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_ready_i(iterator0_desc_ready),
    .desc_valid_o(iterator0_desc_valid),
    .desc_o(iterator0_desc),
    .done_o(iterator0_done)
  );
  block_iterator #(
    .ROLE(1)
  ) u_iterator1 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .start_i(block_start && instr[63:60] < 3),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_ready_i(iterator1_desc_ready),
    .desc_valid_o(iterator1_desc_valid),
    .desc_o(iterator1_desc),
    .done_o(iterator1_done)
  );
  block_iterator #(
    .ROLE(2)
  ) u_iterator2 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .start_i(block_start),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_ready_i(iterator2_desc_ready),
    .desc_valid_o(iterator2_desc_valid),
    .desc_o(iterator2_desc),
    .done_o(iterator2_done)
  );
  block_iterator #(
    .ROLE(3)
  ) u_iterator3 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .start_i(block_start),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_ready_i(iterator3_desc_ready),
    .desc_valid_o(iterator3_desc_valid),
    .desc_o(iterator3_desc),
    .done_o(iterator3_done)
  );
  logic [3:0] issued_q;
  always_ff @(posedge clk) begin
    if (!core_rst_n || block_start) issued_q <= 0;
    else begin
      if (iterator0_done) issued_q[0] <= 1'b1;
      if (iterator1_done || instr[63:60] >= 3) issued_q[1] <= 1'b1;
      if (iterator2_done) issued_q[2] <= 1'b1;
      if (iterator3_done) issued_q[3] <= 1'b1;
    end
  end
  pingpong_ctrl u_input_pingpong (
    .clk(clk),
    .rst_ni(core_rst_n),
    .clear_i(block_start),
    .publish_i(input_pingpong_publish),
    .release_i(input_pingpong_release),
    .produce_ready_o(input_pingpong_produce_ready),
    .consume_valid_o(input_pingpong_consume_valid),
    .produce_bank_o(input_pingpong_produce_bank),
    .consume_bank_o(input_pingpong_consume_bank)
  );
  pingpong_ctrl u_weight_pingpong (
    .clk(clk),
    .rst_ni(core_rst_n),
    .clear_i(block_start),
    .publish_i(weight_pingpong_publish),
    .release_i(weight_pingpong_release),
    .produce_ready_o(weight_pingpong_produce_ready),
    .consume_valid_o(weight_pingpong_consume_valid),
    .produce_bank_o(weight_pingpong_produce_bank),
    .consume_bank_o(weight_pingpong_consume_bank)
  );
  pingpong_ctrl u_coefficient_pingpong (
    .clk(clk),
    .rst_ni(core_rst_n),
    .clear_i(block_start),
    .publish_i(coefficient_pingpong_publish),
    .release_i(coefficient_pingpong_release),
    .produce_ready_o(coefficient_pingpong_produce_ready),
    .consume_valid_o(coefficient_pingpong_consume_valid),
    .produce_bank_o(coefficient_pingpong_produce_bank),
    .consume_bank_o(coefficient_pingpong_consume_bank)
  );
  pingpong_ctrl u_accumulator_pingpong (
    .clk(clk),
    .rst_ni(core_rst_n),
    .clear_i(block_start),
    .publish_i(accumulator_pingpong_publish),
    .release_i(accumulator_pingpong_release),
    .produce_ready_o(accumulator_pingpong_produce_ready),
    .consume_valid_o(accumulator_pingpong_consume_valid),
    .produce_bank_o(accumulator_pingpong_produce_bank),
    .consume_bank_o(accumulator_pingpong_consume_bank)
  );
  input_loader u_input_loader (
    .clk(clk),
    .rst_ni(core_rst_n),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_valid_i(iterator0_desc_valid),
    .desc_i(iterator0_desc),
    .bank_ready_i(input_pingpong_produce_ready),
    .bank_i(input_pingpong_produce_bank),
    .cmd_ready_i(input_loader_cmd_ready),
    .data_valid_i(input_loader_data_valid),
    .data_i(input_loader_data),
    .data_last_i(input_loader_data_last),
    .desc_ready_o(iterator0_desc_ready),
    .cmd_valid_o(input_loader_cmd_valid),
    .cmd_addr_o(input_loader_cmd_addr),
    .cmd_beats_o(input_loader_cmd_beats),
    .data_ready_o(input_loader_data_ready),
    .wr_en_o(input_loader_wr_en),
    .wr_bank_o(input_loader_wr_bank),
    .wr_addr_o(input_loader_wr_addr),
    .wr_data_o(input_loader_wr_data),
    .publish_o(input_pingpong_publish),
    .idle_o(input_loader_idle)
  );
  weight_coef_loader u_weight_loader (
    .clk(clk),
    .rst_ni(core_rst_n),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_valid_i(iterator1_desc_valid),
    .desc_i(iterator1_desc),
    .bank_ready_i(weight_pingpong_produce_ready),
    .bank_i(weight_pingpong_produce_bank),
    .coef_ready_i(coefficient_pingpong_produce_ready),
    .coef_bank_i(coefficient_pingpong_produce_bank),
    .cmd_ready_i(weight_loader_cmd_ready),
    .data_valid_i(weight_loader_data_valid),
    .data_i(weight_loader_data),
    .data_last_i(weight_loader_data_last),
    .desc_ready_o(iterator1_desc_ready),
    .cmd_valid_o(weight_loader_cmd_valid),
    .cmd_addr_o(weight_loader_cmd_addr),
    .cmd_beats_o(weight_loader_cmd_beats),
    .data_ready_o(weight_loader_data_ready),
    .wr_en_o(weight_loader_wr_en),
    .wr_bank_o(weight_loader_wr_bank),
    .wr_array_o(weight_loader_wr_array),
    .wr_addr_o(weight_loader_wr_addr),
    .wr_data_o(weight_loader_wr_data),
    .coef_wr_en_o(weight_loader_coef_wr_en),
    .coef_wr_bank_o(weight_loader_coef_wr_bank),
    .coef_wr_addr_o(weight_loader_coef_wr_addr),
    .publish_o(weight_pingpong_publish),
    .coef_publish_o(coefficient_pingpong_publish),
    .idle_o(weight_loader_idle)
  );
  compute_issue u_compute (
    .clk(clk),
    .rst_ni(core_rst_n),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_valid_i(iterator2_desc_valid),
    .desc_i(iterator2_desc),
    .in_valid_i(input_pingpong_consume_valid),
    .in_bank_i(input_pingpong_consume_bank),
    .w_valid_i(weight_pingpong_consume_valid),
    .w_bank_i(weight_pingpong_consume_bank),
    .acc_ready_i(accumulator_pingpong_produce_ready),
    .acc_bank_i(accumulator_pingpong_produce_bank),
    .vector_allow_i(compute_vector_allow),
    .agu_done_i(compute_agu_done),
    .acc_busy_i(compute_acc_busy),
    .desc_ready_o(iterator2_desc_ready),
    .active_desc_o(compute_active_desc),
    .in_bank_o(compute_in_bank),
    .w_bank_o(compute_w_bank),
    .acc_bank_o(compute_acc_bank),
    .conv_start_o(compute_conv_start),
    .vec_start_o(compute_vec_start),
    .in_release_o(input_pingpong_release),
    .w_release_o(weight_pingpong_release),
    .acc_publish_o(accumulator_pingpong_publish),
    .idle_o(compute_idle)
  );
  drain u_drain (
    .clk(clk),
    .rst_ni(core_rst_n),
    .instr_i(instr),
    .plan_i(plan_bus),
    .desc_valid_i(iterator3_desc_valid),
    .desc_i(iterator3_desc),
    .acc_valid_i(accumulator_pingpong_consume_valid),
    .acc_bank_i(accumulator_pingpong_consume_bank),
    .coef_valid_i(coefficient_pingpong_consume_valid),
    .coef_bank_i(coefficient_pingpong_consume_bank),
    .coef_rdata0_i(drain_coef_rdata0),
    .coef_rdata1_i(drain_coef_rdata1),
    .acc_ready_i(drain_acc_ready),
    .acc_rvalid_i(drain_acc_rvalid),
    .acc_rdata0_i(drain_acc_rdata0),
    .acc_rdata1_i(drain_acc_rdata1),
    .vector_valid_i(drain_vector_valid),
    .vector_data_i(drain_vector_data),
    .cmd_ready_i(drain_cmd_ready),
    .data_ready_i(drain_data_ready),
    .desc_ready_o(iterator3_desc_ready),
    .coef_rd_en_o(drain_coef_rd_en),
    .coef_bank_o(drain_coef_bank),
    .coef_addr0_o(drain_coef_addr0),
    .coef_addr1_o(drain_coef_addr1),
    .acc_rd_valid_o(drain_acc_rd_valid),
    .acc_bank_o(drain_acc_bank),
    .acc_addr_o(drain_acc_addr),
    .acc_rready_o(drain_acc_rready),
    .vector_ready_o(drain_vector_ready),
    .vector_allow_o(compute_vector_allow),
    .cmd_valid_o(drain_cmd_valid),
    .cmd_addr_o(drain_cmd_addr),
    .cmd_beats_o(drain_cmd_beats),
    .data_valid_o(drain_data_valid),
    .data_o(drain_data),
    .acc_release_o(accumulator_pingpong_release),
    .coef_release_o(coefficient_pingpong_release),
    .idle_o(drain_idle)
  );
  assign block_done = !block_start && (&issued_q) && input_loader_idle && weight_loader_idle &&
      compute_idle && drain_idle;
  read_dma u_input_dma (
    .clk(clk),
    .rst_ni(core_rst_n),
    .m_axi_arready(m_axi_in_arready),
    .m_axi_rid(m_axi_in_rid),
    .m_axi_rdata(m_axi_in_rdata),
    .m_axi_rresp(m_axi_in_rresp),
    .m_axi_rlast(m_axi_in_rlast),
    .m_axi_rvalid(m_axi_in_rvalid),
    .cmd_valid_i(input_dma_cmd_valid),
    .cmd_addr_i(input_dma_cmd_addr),
    .cmd_beats_i(input_dma_cmd_beats),
    .data_ready_i(input_dma_data_ready),
    .m_axi_arid(m_axi_in_arid),
    .m_axi_araddr(m_axi_in_araddr),
    .m_axi_arlen(m_axi_in_arlen),
    .m_axi_arsize(m_axi_in_arsize),
    .m_axi_arburst(m_axi_in_arburst),
    .m_axi_arlock(m_axi_in_arlock),
    .m_axi_arcache(m_axi_in_arcache),
    .m_axi_arprot(m_axi_in_arprot),
    .m_axi_arqos(m_axi_in_arqos),
    .m_axi_arregion(m_axi_in_arregion),
    .m_axi_arvalid(m_axi_in_arvalid),
    .m_axi_rready(m_axi_in_rready),
    .cmd_ready_o(input_dma_cmd_ready),
    .data_valid_o(input_dma_data_valid),
    .data_o(input_dma_data),
    .data_last_o(input_dma_data_last),
    .error_o(input_dma_error)
  );
  read_dma u_weight_dma (
    .clk(clk),
    .rst_ni(core_rst_n),
    .m_axi_arready(m_axi_w_arready),
    .m_axi_rid(m_axi_w_rid),
    .m_axi_rdata(m_axi_w_rdata),
    .m_axi_rresp(m_axi_w_rresp),
    .m_axi_rlast(m_axi_w_rlast),
    .m_axi_rvalid(m_axi_w_rvalid),
    .cmd_valid_i(weight_loader_cmd_valid),
    .cmd_addr_i(weight_loader_cmd_addr),
    .cmd_beats_i(weight_loader_cmd_beats),
    .data_ready_i(weight_loader_data_ready),
    .m_axi_arid(m_axi_w_arid),
    .m_axi_araddr(m_axi_w_araddr),
    .m_axi_arlen(m_axi_w_arlen),
    .m_axi_arsize(m_axi_w_arsize),
    .m_axi_arburst(m_axi_w_arburst),
    .m_axi_arlock(m_axi_w_arlock),
    .m_axi_arcache(m_axi_w_arcache),
    .m_axi_arprot(m_axi_w_arprot),
    .m_axi_arqos(m_axi_w_arqos),
    .m_axi_arregion(m_axi_w_arregion),
    .m_axi_arvalid(m_axi_w_arvalid),
    .m_axi_rready(m_axi_w_rready),
    .cmd_ready_o(weight_loader_cmd_ready),
    .data_valid_o(weight_loader_data_valid),
    .data_o(weight_loader_data),
    .data_last_o(weight_loader_data_last),
    .error_o(weight_dma_error)
  );
  assign input_dma_cmd_valid     = layer_cmd_valid || input_loader_cmd_valid;
  assign input_dma_cmd_addr      = layer_cmd_valid ? layer_cmd_addr : input_loader_cmd_addr;
  assign input_dma_cmd_beats     = layer_cmd_valid ? 20'd8 : input_loader_cmd_beats;
  assign layer_cmd_ready         = input_dma_cmd_ready;
  assign input_loader_cmd_ready  = input_dma_cmd_ready && !layer_cmd_valid;
  assign input_dma_data_ready    = layer_data_ready || input_loader_data_ready;
  assign layer_data_valid        = input_dma_data_valid && layer_data_ready;
  assign layer_data              = input_dma_data;
  assign input_loader_data_valid = input_dma_data_valid && !layer_data_ready;
  assign input_loader_data       = input_dma_data;
  assign input_loader_data_last  = input_dma_data_last;
  write_dma u_output_dma (
    .clk(clk),
    .rst_ni(core_rst_n),
    .m_axi_awready(m_axi_out_awready),
    .m_axi_wready(m_axi_out_wready),
    .m_axi_bid(m_axi_out_bid),
    .m_axi_bresp(m_axi_out_bresp),
    .m_axi_bvalid(m_axi_out_bvalid),
    .cmd_valid_i(drain_cmd_valid),
    .cmd_addr_i(drain_cmd_addr),
    .cmd_beats_i(drain_cmd_beats),
    .data_valid_i(drain_data_valid),
    .data_i(drain_data),
    .strb_i(8'hff),
    .m_axi_awid(m_axi_out_awid),
    .m_axi_awaddr(m_axi_out_awaddr),
    .m_axi_awlen(m_axi_out_awlen),
    .m_axi_awsize(m_axi_out_awsize),
    .m_axi_awburst(m_axi_out_awburst),
    .m_axi_awlock(m_axi_out_awlock),
    .m_axi_awcache(m_axi_out_awcache),
    .m_axi_awprot(m_axi_out_awprot),
    .m_axi_awqos(m_axi_out_awqos),
    .m_axi_awregion(m_axi_out_awregion),
    .m_axi_awvalid(m_axi_out_awvalid),
    .m_axi_wdata(m_axi_out_wdata),
    .m_axi_wstrb(m_axi_out_wstrb),
    .m_axi_wlast(m_axi_out_wlast),
    .m_axi_wvalid(m_axi_out_wvalid),
    .m_axi_bready(m_axi_out_bready),
    .cmd_ready_o(drain_cmd_ready),
    .data_ready_o(drain_data_ready),
    .idle_o(output_dma_idle),
    .error_o(output_dma_error)
  );
  logic [7:0] input_pending_q, weight_pending_q;
  always_ff @(posedge clk) begin
    if (!core_rst_n) begin
      input_pending_q  <= 0;
      weight_pending_q <= 0;
    end else begin
      case ({
        input_dma_cmd_valid && input_dma_cmd_ready,
        input_dma_data_valid && input_dma_data_ready && input_dma_data_last
      })
        2'b10:   input_pending_q <= input_pending_q + 1'b1;
        2'b01:   input_pending_q <= input_pending_q - 1'b1;
        default: ;
      endcase
      case ({
        weight_loader_cmd_valid && weight_loader_cmd_ready,
        weight_loader_data_valid && weight_loader_data_ready && weight_loader_data_last
      })
        2'b10:   weight_pending_q <= weight_pending_q + 1'b1;
        2'b01:   weight_pending_q <= weight_pending_q - 1'b1;
        default: ;
      endcase
    end
  end
  assign engines_idle = input_pending_q == 0 && weight_pending_q == 0 && output_dma_idle;
  assign axi_error    = input_dma_error || weight_dma_error || output_dma_error;
  // Layer decode is registered locally before the first AGU start. This
  // removes global opcode fanout/decode from the AGU setup multiplier path.
  logic conv_dw_q;
  always_ff @(posedge clk) begin
    if (block_start) conv_dw_q <= instr[63:60] == 1;
  end
  wire [671:0] compute_desc = compute_active_desc;
  wire         unused_compute_descriptor = ^compute_desc;
  matrix_agu u_matrix_agu (
    .clk(clk),
    .rst_ni(core_rst_n),
    .start_i(compute_conv_start),
    .dw_i(conv_dw_q),
    .in_dense_i(instr[31 +: 1]),
    .subsample_i(plan_bus[212 +: 1]),
    .stride_i(instr[48 +: 4]),
    .kh_i(instr[56 +: 4]),
    .kw_i(instr[52 +: 4]),
    .pad_t_i(instr[44 +: 4]),
    .pad_l_i(instr[40 +: 4]),
    .w_in_i(instr[104 +: 12]),
    .w_out_i(instr[80 +: 12]),
    .cs_i(plan_bus[0 +: 12]),
    .kc_n_i(plan_bus[16 +: 12]),
    .oh0_i(compute_desc[0 +: 12]),
    .rows_i(compute_desc[32 +: 12]),
    .lo_i(compute_desc[64 +: 12]),
    .hi_i(compute_desc[96 +: 12]),
    .m_blk_i(compute_desc[128 +: 10]),
    .q_i(compute_desc[160 +: 3]),
    .pair_i(compute_desc[192 +: 11]),
    .kb0_i(compute_desc[224 +: 16]),
    .kb1_i(compute_desc[256 +: 16]),
    .first_kblk_i(compute_desc[576]),
    .pad_value_i(instr[248 +: 8]),
    .in_rdata0_i(input_rdata0),
    .in_rdata1_i(input_rdata1),
    .w_rdata0_i(weight_rdata0),
    .w_rdata1_i(weight_rdata1),
    .w_ready0_i(array_ready[0]),
    .w_ready1_i(array_ready[1]),
    .shadow_full0_i(array_shadow[0]),
    .shadow_full1_i(array_shadow[1]),
    .done_o(conv_done),
    .busy_o(matrix_agu_busy),
    .in_rd_en_o(matrix_agu_in_rd_en),
    .in_rd_addr0_o(matrix_agu_in_rd_addr0),
    .in_rd_addr1_o(matrix_agu_in_rd_addr1),
    .w_rd_en_o(matrix_agu_w_rd_en),
    .w_rd_addr_o(matrix_agu_w_rd_addr),
    .w_valid_o(matrix_agu_w_valid),
    .w_row0_o(matrix_agu_w_row0),
    .w_row1_o(matrix_agu_w_row1),
    .a_valid_o(matrix_agu_a_valid),
    .a_row0_o(matrix_agu_a_row0),
    .a_row1_o(matrix_agu_a_row1),
    .a_swap_o(matrix_agu_a_swap),
    .a_tag_o(matrix_agu_a_tag)
  );
  assign vector_op = instr[63:60] == 2 ? 2'd0 : (instr[63:60] == 3 ? 2'd1 : 2'd2);
  vector_agu u_vector_agu (
    .clk(clk),
    .rst_ni(core_rst_n),
    .start_i(compute_vec_start),
    .op_i(vector_op),
    .stride_i(instr[48 +: 4]),
    .kh_i(instr[56 +: 4]),
    .kw_i(instr[52 +: 4]),
    .pad_t_i(instr[44 +: 4]),
    .pad_l_i(instr[40 +: 4]),
    .h_in_i(instr[116 +: 12]),
    .w_in_i(instr[104 +: 12]),
    .w_out_i(instr[80 +: 12]),
    .lo_i(compute_desc[64 +: 12]),
    .oh0_i(compute_desc[0 +: 12]),
    .rows_i(compute_desc[32 +: 12]),
    .groups_i(instr[63:60] == 4 ? compute_desc[384 +: 10] : plan_bus[3 +: 10]),
    .npix_i(compute_desc[416 +: 16]),
    .nrows_i(compute_desc[480 +: 16]),
    .pad_value_i(instr[248 +: 8]),
    .in_rdata0_i(input_rdata0),
    .in_rdata1_i(input_rdata1),
    .w_rdata0_i(weight_rdata0),
    .w_rdata1_i(weight_rdata1),
    .v_ready_i(vector_agu_v_ready),
    .done_o(vec_done),
    .busy_o(unused_vector_agu_busy),
    .in_rd_en_o(vector_agu_in_rd_en),
    .in_rd_addr0_o(vector_agu_in_rd_addr0),
    .in_rd_addr1_o(vector_agu_in_rd_addr1),
    .w_rd_en_o(vector_agu_w_rd_en),
    .w_rd_addr_o(vector_agu_w_rd_addr),
    .v_valid_o(vector_agu_v_valid),
    .v_a_o(vector_agu_v_a),
    .v_b_o(vector_agu_v_b),
    .v_first_o(vector_agu_v_first),
    .v_last_o(vector_agu_v_last)
  );
  vector_unit u_vector (
    .clk(clk),
    .rst_ni(core_rst_n),
    .op_i(vector_op),
    .zp_a_i(instr[160 +: 8]),
    .zp_b_i(instr[152 +: 8]),
    .zp_out_i(instr[144 +: 8]),
    .act_min_i(instr[136 +: 8]),
    .act_max_i(instr[128 +: 8]),
    .scalar0_i(instr[288 +: 32]),
    .scalar1_i(instr[256 +: 32]),
    .in_valid_i(vector_agu_v_valid),
    .in_a_i(vector_agu_v_a),
    .in_b_i(vector_agu_v_b),
    .in_first_i(vector_agu_v_first),
    .in_last_i(vector_agu_v_last),
    .out_ready_i(drain_vector_ready),
    .in_ready_o(vector_agu_v_ready),
    .out_valid_o(drain_vector_valid),
    .out_data_o(drain_vector_data)
  );
  assign compute_agu_done = conv_done || vec_done;
  assign compute_acc_busy = acc_busy;
  assign input_rd_en = instr[63:60] < 2 ? matrix_agu_in_rd_en : vector_agu_in_rd_en;
  assign input_addr0 = instr[63:60] < 2 ? matrix_agu_in_rd_addr0 : vector_agu_in_rd_addr0;
  assign input_addr1 = instr[63:60] < 2 ? matrix_agu_in_rd_addr1 : vector_agu_in_rd_addr1;
  assign weight_rd_en = instr[63:60] < 2 ? {2{matrix_agu_w_rd_en}} :
      {(vector_agu_w_rd_en && vector_agu_w_rd_addr[10]),
        (vector_agu_w_rd_en && !vector_agu_w_rd_addr[10])};
  assign weight_addr = instr[63:60] < 2 ? matrix_agu_w_rd_addr : vector_agu_w_rd_addr[9:0];
  wire [63:0] input_data [2][2];
  wire [63:0] weight_data[2][2];
  wire [63:0] coef_data  [2][2];
  assign input_rdata0      = input_data[compute_in_bank][0];
  assign input_rdata1      = input_data[compute_in_bank][1];
  assign weight_rdata0     = weight_data[compute_w_bank][0];
  assign weight_rdata1     = weight_data[compute_w_bank][1];
  assign drain_coef_rdata0 = coef_data[drain_coef_bank][0];
  assign drain_coef_rdata1 = coef_data[drain_coef_bank][1];
  ram_2p #(
    .WIDTH(64),
    .DEPTH(8192)
  ) u_input0 (
    .clk(clk),
    .a_en_i((compute_in_bank == 1'd0) && input_rd_en[0]),
    .a_we_i(1'b0),
    .a_addr_i(input_addr0),
    .a_wdata_i(64'b0),
    .a_rdata_o(input_data[0][0]),
    .b_en_i((input_loader_wr_en && input_loader_wr_bank == 1'd0) || ((compute_in_bank == 1'd0)
        && input_rd_en[1])),
    .b_we_i((input_loader_wr_en && input_loader_wr_bank == 1'd0)),
    .b_addr_i((input_loader_wr_en && input_loader_wr_bank == 1'd0) ? input_loader_wr_addr :
        input_addr1),
    .b_wdata_i(input_loader_wr_data),
    .b_rdata_o(input_data[0][1])
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(1024)
  ) u_weight0_0 (
    .clk(clk),
    .a_en_i(weight_rd_en[0] && compute_w_bank == 1'd0),
    .a_we_i(1'b0),
    .a_addr_i(weight_addr),
    .a_wdata_i(64'b0),
    .a_rdata_o(weight_data[0][0]),
    .b_en_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd0 && weight_loader_wr_array ==
        1'd0)),
    .b_we_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd0 && weight_loader_wr_array ==
        1'd0)),
    .b_addr_i(weight_loader_wr_addr),
    .b_wdata_i(weight_loader_wr_data),
    .b_rdata_o(unused_unused_weight0_0)
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(1024)
  ) u_weight0_1 (
    .clk(clk),
    .a_en_i(weight_rd_en[1] && compute_w_bank == 1'd0),
    .a_we_i(1'b0),
    .a_addr_i(weight_addr),
    .a_wdata_i(64'b0),
    .a_rdata_o(weight_data[0][1]),
    .b_en_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd0 && weight_loader_wr_array ==
        1'd1)),
    .b_we_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd0 && weight_loader_wr_array ==
        1'd1)),
    .b_addr_i(weight_loader_wr_addr),
    .b_wdata_i(weight_loader_wr_data),
    .b_rdata_o(unused_unused_weight0_1)
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(64)
  ) u_coefficient0 (
    .clk(clk),
    .a_en_i((weight_loader_coef_wr_en && weight_loader_coef_wr_bank == 1'd0) ||
        (drain_coef_rd_en && drain_coef_bank == 1'd0)),
    .a_we_i((weight_loader_coef_wr_en && weight_loader_coef_wr_bank == 1'd0)),
    .a_addr_i((weight_loader_coef_wr_en && weight_loader_coef_wr_bank == 1'd0) ?
        weight_loader_coef_wr_addr : drain_coef_addr0),
    .a_wdata_i(weight_loader_wr_data),
    .a_rdata_o(coef_data[0][0]),
    .b_en_i((drain_coef_rd_en && drain_coef_bank == 1'd0)),
    .b_we_i(1'b0),
    .b_addr_i(drain_coef_addr1),
    .b_wdata_i(64'b0),
    .b_rdata_o(coef_data[0][1])
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(8192)
  ) u_input1 (
    .clk(clk),
    .a_en_i((compute_in_bank == 1'd1) && input_rd_en[0]),
    .a_we_i(1'b0),
    .a_addr_i(input_addr0),
    .a_wdata_i(64'b0),
    .a_rdata_o(input_data[1][0]),
    .b_en_i((input_loader_wr_en && input_loader_wr_bank == 1'd1) || ((compute_in_bank == 1'd1)
        && input_rd_en[1])),
    .b_we_i((input_loader_wr_en && input_loader_wr_bank == 1'd1)),
    .b_addr_i((input_loader_wr_en && input_loader_wr_bank == 1'd1) ? input_loader_wr_addr :
        input_addr1),
    .b_wdata_i(input_loader_wr_data),
    .b_rdata_o(input_data[1][1])
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(1024)
  ) u_weight1_0 (
    .clk(clk),
    .a_en_i(weight_rd_en[0] && compute_w_bank == 1'd1),
    .a_we_i(1'b0),
    .a_addr_i(weight_addr),
    .a_wdata_i(64'b0),
    .a_rdata_o(weight_data[1][0]),
    .b_en_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd1 && weight_loader_wr_array ==
        1'd0)),
    .b_we_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd1 && weight_loader_wr_array ==
        1'd0)),
    .b_addr_i(weight_loader_wr_addr),
    .b_wdata_i(weight_loader_wr_data),
    .b_rdata_o(unused_unused_weight1_0)
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(1024)
  ) u_weight1_1 (
    .clk(clk),
    .a_en_i(weight_rd_en[1] && compute_w_bank == 1'd1),
    .a_we_i(1'b0),
    .a_addr_i(weight_addr),
    .a_wdata_i(64'b0),
    .a_rdata_o(weight_data[1][1]),
    .b_en_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd1 && weight_loader_wr_array ==
        1'd1)),
    .b_we_i((weight_loader_wr_en && weight_loader_wr_bank == 1'd1 && weight_loader_wr_array ==
        1'd1)),
    .b_addr_i(weight_loader_wr_addr),
    .b_wdata_i(weight_loader_wr_data),
    .b_rdata_o(unused_unused_weight1_1)
  );
  ram_2p #(
    .WIDTH(64),
    .DEPTH(64)
  ) u_coefficient1 (
    .clk(clk),
    .a_en_i((weight_loader_coef_wr_en && weight_loader_coef_wr_bank == 1'd1) ||
        (drain_coef_rd_en && drain_coef_bank == 1'd1)),
    .a_we_i((weight_loader_coef_wr_en && weight_loader_coef_wr_bank == 1'd1)),
    .a_addr_i((weight_loader_coef_wr_en && weight_loader_coef_wr_bank == 1'd1) ?
        weight_loader_coef_wr_addr : drain_coef_addr0),
    .a_wdata_i(weight_loader_wr_data),
    .a_rdata_o(coef_data[1][0]),
    .b_en_i((drain_coef_rd_en && drain_coef_bank == 1'd1)),
    .b_we_i(1'b0),
    .b_addr_i(drain_coef_addr1),
    .b_wdata_i(64'b0),
    .b_rdata_o(coef_data[1][1])
  );
  ws_array #(
    .TAG_W(11)
  ) u_array0 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .w_valid_i(matrix_agu_w_valid),
    .w_row_i(matrix_agu_w_row0),
    .a_valid_i(matrix_agu_a_valid),
    .a_row_i(matrix_agu_a_row0),
    .a_swap_i(matrix_agu_a_swap),
    .a_tag_i({compute_acc_bank, matrix_agu_a_tag}),
    .w_ready_o(array_ready[0]),
    .shadow_full_o(array_shadow[0]),
    .c_valid_o(array0_valid),
    .c_row_o(array0_row),
    .c_tag_o(array0_tag)
  );
  accumulator u_accumulator0 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .wr_valid_i(array0_valid),
    .wr_bank_i(array0_tag[10]),
    .wr_addr_i(array0_tag[8:0]),
    .wr_accum_i(array0_tag[9]),
    .wr_data_i(array0_row),
    .rd_valid_i(drain_acc_rd_valid[0]),
    .rd_bank_i(drain_acc_bank),
    .rd_addr_i(drain_acc_addr),
    .rdata_ready_i(drain_acc_rready[0]),
    .rd_ready_o(drain_acc_ready[0]),
    .rdata_valid_o(drain_acc_rvalid[0]),
    .rdata_o(drain_acc_rdata0),
    .busy_o(acc_busy[0])
  );
  ws_array #(
    .TAG_W(11)
  ) u_array1 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .w_valid_i(matrix_agu_w_valid),
    .w_row_i(matrix_agu_w_row1),
    .a_valid_i(matrix_agu_a_valid),
    .a_row_i(matrix_agu_a_row1),
    .a_swap_i(matrix_agu_a_swap),
    .a_tag_i({compute_acc_bank, matrix_agu_a_tag}),
    .w_ready_o(array_ready[1]),
    .shadow_full_o(array_shadow[1]),
    .c_valid_o(array1_valid),
    .c_row_o(array1_row),
    .c_tag_o(array1_tag)
  );
  accumulator u_accumulator1 (
    .clk(clk),
    .rst_ni(core_rst_n),
    .wr_valid_i(array1_valid),
    .wr_bank_i(array1_tag[10]),
    .wr_addr_i(array1_tag[8:0]),
    .wr_accum_i(array1_tag[9]),
    .wr_data_i(array1_row),
    .rd_valid_i(drain_acc_rd_valid[1]),
    .rd_bank_i(drain_acc_bank),
    .rd_addr_i(drain_acc_addr),
    .rdata_ready_i(drain_acc_rready[1]),
    .rd_ready_o(drain_acc_ready[1]),
    .rdata_valid_o(drain_acc_rvalid[1]),
    .rdata_o(drain_acc_rdata1),
    .busy_o(acc_busy[1])
  );
  assign perf_events = {
    (drain_data_valid && !drain_data_ready),
    (matrix_agu_busy && !matrix_agu_a_valid),
    !output_dma_idle,
    (weight_pending_q != 0),
    (input_pending_q != 0),
    matrix_agu_a_valid,
    layer_busy
  };
endmodule
`default_nettype wire
