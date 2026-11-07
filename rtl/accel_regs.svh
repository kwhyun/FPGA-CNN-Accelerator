// Auto-generated from data/regmap.yaml by compiler/regmap/generate.py.
// DO NOT EDIT BY HAND — regenerate via `python -m compiler.regmap.generate`.

// AXI-Lite: 12-bit address, 32-bit data.

// ----- Register offsets -----
localparam logic [11:0] CTRL_OFFSET = 12'h000;
localparam logic [11:0] STATUS_OFFSET = 12'h004;
localparam logic [11:0] IRQ_EN_OFFSET = 12'h008;
localparam logic [11:0] IRQ_STATUS_OFFSET = 12'h00C;
localparam logic [11:0] INSTR_BASE_OFFSET = 12'h010;
localparam logic [11:0] INSTR_COUNT_OFFSET = 12'h014;
localparam logic [11:0] LAYER_INDEX_OFFSET = 12'h018;
localparam logic [11:0] ERROR_CODE_OFFSET = 12'h01C;
localparam logic [11:0] PERF_SELECT_OFFSET = 12'h020;
localparam logic [11:0] PERF_DATA_OFFSET = 12'h024;
localparam logic [11:0] HW_VERSION_OFFSET = 12'h028;

// ----- Field bit positions -----
localparam int unsigned CTRL_START_BIT = 0;
localparam int unsigned CTRL_SOFT_RESET_BIT = 1;
localparam int unsigned STATUS_BUSY_BIT = 0;
localparam int unsigned STATUS_DONE_BIT = 1;
localparam int unsigned STATUS_ERROR_BIT = 2;
localparam int unsigned IRQ_EN_DONE_BIT = 0;
localparam int unsigned IRQ_EN_ERROR_BIT = 1;
localparam int unsigned IRQ_STATUS_DONE_BIT = 0;
localparam int unsigned IRQ_STATUS_ERROR_BIT = 1;
