`default_nettype none
// Two-entry monotonic producer/consumer semaphore. A producer owns its bank
// until publish_i; a consumer owns its bank until release_i. No RAM is reset.
module pingpong_ctrl (
  input  wire clk,
  input  wire rst_ni,
  input  wire clear_i,
  input  wire publish_i,
  input  wire release_i,
  output wire produce_ready_o,
  output wire consume_valid_o,
  output wire produce_bank_o,
  output wire consume_bank_o
);
  logic [31:0] produced_q, consumed_q;
  assign produce_ready_o = (produced_q - consumed_q) < 32'd2;
  assign consume_valid_o = produced_q != consumed_q;
  assign produce_bank_o  = produced_q[0];
  assign consume_bank_o  = consumed_q[0];
  always_ff @(posedge clk) begin
    if (!rst_ni || clear_i) begin
      produced_q <= '0;
      consumed_q <= '0;
    end else begin
      if (publish_i) produced_q <= produced_q + 1'b1;
      if (release_i) consumed_q <= consumed_q + 1'b1;
    end
  end
`ifndef SYNTHESIS
  always_ff @(posedge clk) begin
    if (rst_ni && !clear_i) begin
      assert (!publish_i || produce_ready_o);
      assert (!release_i || consume_valid_o);
    end
  end
`endif
endmodule
`default_nettype wire
