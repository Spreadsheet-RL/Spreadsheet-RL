#include <torch/extension.h>

__global__ void forward_kernel(const float *input, float *output, int M, int N) {
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    output[x * M + y] = input[y * N + x];
}

torch::Tensor forward(torch::Tensor input) {
    assert(input.ndimension() == 2);
    const int64_t M = input.size(0);
    const int64_t N = input.size(1);
    std::vector<int64_t> output_shape{N, M};
    torch::Tensor output = torch::empty(output_shape, input.options());

    dim3 threads(32, 32);
    dim3 blocks(N / 32, M / 32);
    forward_kernel<<<blocks, threads>>>(input.const_data_ptr<float>(), output.mutable_data_ptr<float>(), M, N);
    return output;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) { m.def("forward", &forward); }
