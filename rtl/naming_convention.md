# RTL Naming Conventions

> Based on the **lowRISC / OpenTitan Style Guide** (*de facto* industry standard)

Consistent RTL naming makes large-scale integration possible and greatly improves readability and maintainability.

---

## 1. Signals and Types

### 1.1 Signal Names
- Always use **snake_case** (lowercase + underscores)
- Correct: `data_valid`, `write_enable`, `byte_count`
- Incorrect: `dataValid`, `WriteEnable`
- No single-letter names except `genvar` loop variables

### 1.2 Clock and Reset
- **Clock**
  - Single clock: `clk`
  - Multiple clock domains: `clk_<domain>` (e.g. `clk_axi`, `clk_pixel`)
- **Reset**
  - `rst_n`: active-low **synchronous** reset. Do not use an async sensitivity list, because DSP48E1 and BRAM internal registers support only sync reset. Sequential blocks use the form `always_ff @(posedge clk) begin if (!rst_ni) ... end`

### 1.3 Active-Low Signals
- Mark active-low with the `_n` suffix (e.g. `cs_n`, `wr_n`, `oe_n`)
- Every signal without `_n` is **active-high** by default

### 1.4 Flip-Flop Naming: `_d` / `_q` Convention
| Suffix | Meaning |
|--------|------|
| `_d`   | combinational next-state input to the flip-flop D terminal |
| `_q`   | registered output from the flip-flop Q terminal |

- Example: `state_d` goes into the register and `state_q` comes out
- Pipeline stages: `data_q`, `data_q2`, `data_q3`

---

## 2. Modules, Instances, Ports

### 2.1 Module Names
- Lowercase + underscores (snake_case): `uart_core`, `fifo_controller`, `alu_top`
- **One module per file**, with the file name equal to the module name (e.g. `uart_core.v`)

### 2.2 Instance Names
- Use the `u_` prefix ("unit"): `u_fifo`, `u_alu`, `u_uart_core`
- **Do not use** the `i_` prefix, which conflicts with the input port convention
- Number arrayed instances: `u_stage0`, `u_stage1`, `u_stage2`

### 2.3 Port Direction Suffixes (SystemVerilog / lowRISC style)
| Suffix | Direction |
|--------|------|
| `_i`   | input |
| `_o`   | output |
| `_io`  | bidirectional |

- Examples: `data_valid_i`, `data_ready_o`, `sda_io`
- Combined with active-low: `rst_ni` (✓), `rst_n_i` (✗)

### 2.4 Parameters and Constants
- All **UPPER_CASE**: `ADDR_WIDTH`, `DATA_DEPTH`, `IDLE`, `RUN`
- The same rule applies to `parameter` and `localparam`

---

## 3. Quick Reference

### 3.1 Case and Style
| Target | Style |
|------|--------|
| Signals, modules, instances | `snake_case` |
| Parameters, localparams | `UPPER_CASE` |

### 3.2 Suffixes
| Suffix | Meaning |
|--------|------|
| `_n`   | active-low signal |
| `_d`   | combinational input to flip-flop (D terminal) |
| `_q`   | registered output of flip-flop (Q terminal) |
| `_i` / `_o` / `_io` | port direction (input / output / bidirectional) |

### 3.3 Prefixes
| Prefix | Meaning |
|--------|------|
| `u_`   | module instance (e.g. `u_fifo`, `u_alu`) |
| `clk_` | clock domain (e.g. `clk_axi`, `clk_sys`) |
| `rst_` | reset (e.g. `rst_n`, `rst_sys_n`) |

### 3.4 Suffix Combination Order
- Active-low `_n` comes **before** the direction `_i` / `_o`
- No extra underscore between the two suffixes
- Examples: `rst_ni`, `cs_no`

### 3.5 File Organization
- One module per file; the file name must match the module name
- Put `` `default_nettype none `` at the top of every file
