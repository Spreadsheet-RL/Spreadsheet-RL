#include "ATen/ops/empty_like.h"
#include "kernel.cuh"
#include "utils.h"
#include <ATen/ATen.h>
#include <cuda_runtime.h>
#include <iostream>
#include <torch/nn/functional.h>

void layer_norm(float *input, float *output, float *gamma, float *beta, int N,
                int feature_size);

inline std::pair<float, float> compute_sol(float duration_ms,
                                           size_t input_size) {
  float compute_sol = input_size / kA100TOPS / 1E9;
  float memory_sol = float(input_size) * sizeof(float) / kA100MemBandwidth / 1E6;
  return {compute_sol / duration_ms,
          std::max<float>(compute_sol, memory_sol) / duration_ms}; // 'mfu', sol
}

int main() {
  int n = 10000, f = 2048;
  float gamma = 1, beta = 0;

  auto input = at::randn({n, f}, at::kCUDA);
  auto output_torch = torch::nn::functional::layer_norm(
      input, torch::nn::functional::LayerNormFuncOptions({f}));
  auto profiler = Profiler();

  auto input_ptr = reinterpret_cast<float *>(input.data_ptr());
  auto output_cuda = at::empty_like(output_torch);
  auto output_ptr = reinterpret_cast<float *>(output_cuda.data_ptr());

  float total_time = 0;

  int warmup = 10, repeat = 10;
  // warmup
  for (int i = 0; i < warmup; i++) {
    layer_norm(input_ptr, output_ptr, &gamma, &beta, n, f);
  }

  // run kernel
  for (int i = 0; i < repeat; i++) {
    profiler.start();
    layer_norm(input_ptr, output_ptr, &gamma, &beta, n, f);
    total_time += profiler.get_elasped(); // ms
  }
  float avg_time = total_time / repeat;

  // check result
  auto result = Result();
  result.bound_type = BoundType::Memory;
  result.is_correct = at::allclose(output_cuda, output_torch, 0.001, 0.001);
  auto mfu_sol = compute_sol(avg_time, n * f + 2 * f);

  result.sol = mfu_sol.second;
  result.is_too_slow = result.sol < 0.3;

  std::cout << result;
  return 0;
}
