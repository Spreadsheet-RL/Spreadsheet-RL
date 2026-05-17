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

import torch


def verify_model(model, custom_model, input_data):
    if input_data is None:
        input_data = []
    if not isinstance(input_data, list):
        input_data = [input_data]

    for i, item in enumerate(input_data):
        if isinstance(item, torch.Tensor):
            input_data[i] = item.cuda()

    out = model(*input_data)
    custom_out = custom_model(*input_data)
    torch.testing.assert_close(out, custom_out)
    print("verify_model ok")
