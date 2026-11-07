// Board inference test: N ImageNet images from DDR (images.bin), Top-1/Top-5 and latency over UART.
// model.bin (instructions, weights, coefficients, tensor space) and images.bin are loaded by FSBL.
#include <stdint.h>

#include "accel_regs.h"
#include "board_model.h"
#include "xil_cache.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "sleep.h"
#include "xiltimer.h"

#define ACCEL_BASE 0x40000000u
// The BSP's COUNTS_PER_SECOND expands to `CPU_HZ/2` without parentheses. Evaluate it to a value first.
static const uint64_t TIMER_HZ = (uint64_t)(COUNTS_PER_SECOND);
#define TIMEOUT_TICKS (TIMER_HZ * 5u)

static inline uint32_t rd(uint32_t off) { return Xil_In32(ACCEL_BASE + off); }
static inline void wr(uint32_t off, uint32_t v) { Xil_Out32(ACCEL_BASE + off, v); }

static uint32_t perf(uint32_t sel) {
  wr(ACCEL_PERF_SELECT_OFFSET, sel);
  return rd(ACCEL_PERF_DATA_OFFSET);
}

// Descending; ties go to the lower class (same as topk in compiler/golden/run.py)
static void top5(const volatile int8_t *logits, uint32_t n, int32_t out[5]) {
  for (int k = 0; k < 5; k++) out[k] = -1;
  for (uint32_t c = 0; c < n; c++) {
    int8_t v = logits[c];
    for (int k = 0; k < 5; k++) {
      if (out[k] < 0 || v > logits[out[k]]) {
        for (int j = 4; j > k; j--) out[j] = out[j - 1];
        out[k] = (int32_t)c;
        break;
      }
    }
  }
}

// Print num/den as a percentage (two decimals)
static void print_pct(uint32_t num, uint32_t den) {
  uint32_t bp = den ? (uint32_t)(((uint64_t)num * 10000u + den / 2u) / den) : 0;
  xil_printf("%3d.%02d%%", bp / 100u, bp % 100u);
}

int main(void) {
  Xil_DCacheDisable();  // DDR shared with the accelerator: disable the cache instead of managing coherence
  usleep(1u);           // The first usleep initializes the global timer, so call it once before measuring
  xil_printf("\r\nFPGA-CNN-Accelerator inference test: %s, %d images\r\n", MODEL_NAME, N_IMAGES);
  xil_printf("HW_VERSION 0x%08x\r\n", rd(ACCEL_HW_VERSION_OFFSET));
  wr(ACCEL_INSTR_BASE_OFFSET, INSTR_ADDR);
  wr(ACCEL_INSTR_COUNT_OFFSET, INSTR_COUNT);
  uint32_t top1 = 0, top5_ok = 0, errors = 0;
  uint64_t ticks_sum = 0, cycles_sum = 0;
  for (uint32_t i = 0; i < N_IMAGES; i++) {
    Xil_Out32(INSTR0_IN_BASE_ADDR, IMAGES_DDR_BASE + i * IMAGE_STRIDE);  // first-layer input address
    XTime t0, t1;
    XTime_GetTime(&t0);
    wr(ACCEL_CTRL_OFFSET, ACCEL_CTRL_START_MASK);
    uint32_t st;
    do {
      st = rd(ACCEL_STATUS_OFFSET);
      XTime_GetTime(&t1);
    } while (!(st & (ACCEL_STATUS_DONE_MASK | ACCEL_STATUS_ERROR_MASK)) && (t1 - t0) < TIMEOUT_TICKS);
    if (!(st & ACCEL_STATUS_DONE_MASK) || (st & ACCEL_STATUS_ERROR_MASK)) {
      xil_printf("image %d: status 0x%x layer %d error_code %d\r\n", i, st, rd(ACCEL_LAYER_INDEX_OFFSET),
                 rd(ACCEL_ERROR_CODE_OFFSET));
      errors++;
      wr(ACCEL_CTRL_OFFSET, ACCEL_CTRL_SOFT_RESET_MASK);
      continue;
    }
    ticks_sum += (uint64_t)(t1 - t0);
    cycles_sum += perf(0);
    int32_t t5[5];
    top5((const volatile int8_t *)LOGITS_ADDR, NUM_CLASSES, t5);
    int32_t gt = (int32_t)image_label[i] + (int32_t)CLASS_OFFSET;
    top1 += (t5[0] == gt);
    for (int k = 0; k < 5; k++) top5_ok += (t5[k] == gt);
    uint32_t seen = i + 1;
    xil_printf("[%4d/%d] Top-1 ", seen, N_IMAGES);
    print_pct(top1, seen);
    xil_printf("  Top-5 ");
    print_pct(top5_ok, seen);
    xil_printf("\r\n");
  }
  uint32_t done = N_IMAGES - errors;
  uint32_t mean_us = done ? (uint32_t)(ticks_sum * 1000000u / TIMER_HZ / done) : 0;
  uint32_t mean_cyc = done ? (uint32_t)(cycles_sum / done) : 0;
  xil_printf("Top-1 %d/%d, Top-5 %d/%d, errors %d\r\n", top1, N_IMAGES, top5_ok, N_IMAGES, errors);
  xil_printf("mean latency %d.%03d ms (PS timer), mean accelerator cycles %d\r\n", mean_us / 1000,
             mean_us % 1000, mean_cyc);
  xil_printf("perf (last image): total %d, rows %d, hp_in %d, hp_w %d, hp_out %d, agu_wait %d, drain_wait %d\r\n",
             perf(0), perf(1), perf(2), perf(3), perf(4), perf(5), perf(6));
  return 0;
}
