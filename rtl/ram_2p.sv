// True dual-port BRAM: both ports read/write, synchronous read (1 cycle), read during a write on the same port returns the old value.
`default_nettype none
module ram_2p #(
  parameter int unsigned WIDTH = 64,
  parameter int unsigned DEPTH = 1024,
  parameter int unsigned ADDR_W = $clog2(DEPTH)
) (
  input  wire                clk,
  // Port A
  input  wire                a_en_i,
  input  wire                a_we_i,
  input  wire [ADDR_W-1:0]   a_addr_i,
  input  wire [ WIDTH-1:0]   a_wdata_i,
  output logic [ WIDTH-1:0]  a_rdata_o,
  // Port B
  input  wire                b_en_i,
  input  wire                b_we_i,
  input  wire [ADDR_W-1:0]   b_addr_i,
  input  wire [ WIDTH-1:0]   b_wdata_i,
  output logic [ WIDTH-1:0]  b_rdata_o
);
  (* ram_style = "block" *) logic [WIDTH-1:0] mem[DEPTH];

  always_ff @(posedge clk) begin
    if (a_en_i) begin
      if (a_we_i) mem[a_addr_i] <= a_wdata_i;
      a_rdata_o <= mem[a_addr_i];
    end
  end

  always_ff @(posedge clk) begin
    if (b_en_i) begin
      if (b_we_i) mem[b_addr_i] <= b_wdata_i;
      b_rdata_o <= mem[b_addr_i];
    end
  end
endmodule
`default_nettype wire
