#pragma once

#include <ATen/ATen.h>
#include <cuda_runtime.h>
#include <ostream>
#include <string>

constexpr int kAlphabetSize = 26;
constexpr float kA100TOPS = 19.5; // TOPS for int32
constexpr int kA100MemBandwidth = 2039.0; // GB/s

enum BoundType {
  Memory,
  Compute
};

struct Result {
  bool is_correct = false;
  bool is_too_slow = false;
  float sol = 0;
  BoundType bound_type = BoundType::Memory;
};

inline std::ostream &operator<<(std::ostream &os, const Result &res) {

  auto append_str = [&os](auto tag, auto value) {
    os << tag;
    os << ":";
    os << value;
    os << ",";
  };

  append_str("bound_type", res.bound_type == BoundType::Memory? "memory": "compute");
  append_str("is_correct", res.is_correct ? "true" : "false");
  append_str("is_too_slow", res.is_too_slow ? "true" : "false");
  append_str("sol", std::to_string(res.sol).c_str());

  os << std::endl;
}

class Profiler {
public:
  void start() {
    cudaEventCreate(&start_);
    cudaEventCreate(&stop_);
    cudaEventRecord(start_);
  }
  float get_elasped() {
    cudaEventRecord(stop_);
    cudaEventSynchronize(stop_);
    float milliseconds = 0;
    cudaEventElapsedTime(&milliseconds, start_, stop_);
    return milliseconds;
  }
  void reset_timer() {
    cudaEventDestroy(start_);
    cudaEventDestroy(stop_);
  }

private:
  cudaEvent_t start_, stop_;
};
