# RTL Code Style

Layout rules applied together with the RTL naming conventions.

- Indent with 2 spaces. Align with spaces instead of tabs so the code looks the same regardless of editor settings.
- Declare one port per line, ordered as clock/reset inputs, other inputs, then outputs.
- Within each direction, group ports by role: AXI, run control, layer config, shape, data streams, coefficients, status/results. Keep AXI signals of the same interface and direction contiguous.
- Put one blank line between groups, with a comment stating role and direction directly above, such as `// AXI HP1 master input`. Do not repeat the same description on each signal.
- Instances use named connections, in the same group order as the module port declaration. Write one connection per line and align the opening parentheses across the whole instance with spaces.
- Keep `valid`, `ready` and `data` of one transfer in the same stream group. If they differ in direction, put them in the same-named group of that direction.
- Internal signal names reflect the owning module or connection path. Examples: `bias_push_*`, `instruction_buffer_push_*`, `quantization_push_*`, `quantization_pop_*`, `residual_buffer_*`, `matrix_array_*`.
- Keep `rq_bias_base_*`, since it means the instruction/host address of requantization+bias data. External AXI port names, generated register definitions and existing parameter names stay aligned with the integrated interface.
- Put a short comment describing the role above each logic unit (mux, state transition, address/coordinate generation, queue update, pipeline). Do not annotate every self-evident assignment.
- Style changes must not change expressions, constants, bit widths/signedness, reset style, clock/pipeline boundaries, memory attributes or handshake conditions.

The shared format settings are in `.verible-format` at the repository root. Group port connections by the rules above first, then align the parentheses across the instance. The formatter preserves this alignment.

```bash
verible-verilog-format --flagfile=.verible-format --inplace rtl/*.sv rtl/accel_top.v
verible-verilog-lint --rules_config=.rules.verible_lint rtl/*.sv rtl/accel_top.v
```
