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

import torch.utils.cpp_extension

src_path = Path("transpose.cu")
build_dir = Path("build")
build_dir.mkdir(parents=True, exist_ok=True)

torch.utils.cpp_extension.load(
    name=src_path.stem,
    sources=[src_path],
    extra_cuda_cflags=["-O3"],
    build_directory=build_dir,
    verbose=True,
)
