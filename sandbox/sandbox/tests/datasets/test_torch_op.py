# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest
from fastapi.testclient import TestClient

from sandbox.datasets.types import Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)


@pytest.mark.cuda
async def test_torch_op_get():
    request = GetPromptsRequest(dataset='torch_op', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    print(results)


@pytest.mark.cuda
async def test_torch_op_get_id():
    request = GetPromptByIdRequest(dataset='torch_op', id='byteir/abs/abs_1', config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)


@pytest.mark.cuda
async def test_torch_op_submit():
    request = SubmitRequest(dataset='torch_op',
                            id='byteir/abs/abs_1',
                            config=TestConfig(),
                            completion='''
```cpp
#include <torch/extension.h>

namespace {

__global__ void Unknown0_kernel_Unknown0_kernel(float* v1, float* v2, int32_t v3) {
  int32_t bidx = blockIdx.x;
  int32_t bdimx = blockDim.x;
  int32_t tidx = threadIdx.x;
  int32_t v4 = bdimx * bidx;
  int32_t v5 = tidx + v4;
  int32_t gdimx = gridDim.x;
  int32_t v6 = bdimx * gdimx;
  for (int32_t idx7 = v5; idx7 < v3; idx7 += v6) {
    float* v8 = v1 + idx7;
    float v9 = *v8;
    float v10 = fabsf(v9);
    float* v11 = v2 + idx7;
    *v11 = v10;
  }
  return;
}


void forward(torch::Tensor v1, torch::Tensor v2) {
  int32_t v3 = v1.size(1);
  float* v4 = v1.data_ptr<float>();
  int32_t v5 = v1.size(0);
  float* v6 = v2.data_ptr<float>();
  int32_t v7 = v5 * v3;
  int32_t v8 = (v7 + 1024 - 1) / 1024;
  int32_t max9 = max(v8, 1);
  Unknown0_kernel_Unknown0_kernel<<<dim3(max9, 1, 1), dim3(256, 1, 1)>>>(v4, v6, v7);
  return;
}



} // namespace

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("forward", &forward);
}
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    assert response.json()['accepted'] == True
