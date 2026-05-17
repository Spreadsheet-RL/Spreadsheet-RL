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

from pathlib import Path

import torch
import torch.utils.cpp_extension
from test_utils import verify_model
from torch.utils.benchmark import Timer


def forward(input: torch.Tensor) -> torch.Tensor:
    return input.T


src_path = Path(f"transpose.cu")
cuda_op = torch.utils.cpp_extension.load(
    name=src_path.stem,
    sources=[src_path],
    extra_cuda_cflags=["-O3"],
    build_directory="build",
    verbose=True,
)
cuda_forward = cuda_op.forward

# check correctness
inputs = torch.randn(1024, 2048, dtype=torch.float32, device="cuda")
verify_model(forward, cuda_forward, inputs)

# check performance
print(Timer("forward(inputs)", globals=globals()).timeit(10))
print(Timer("cuda_forward(inputs)", globals=globals()).timeit(10))
