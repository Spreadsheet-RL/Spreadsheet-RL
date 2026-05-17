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

import base64
import os

import pytest
from fastapi.testclient import TestClient

from sandbox.runners import CommandRunStatus
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.server.server import app

client = TestClient(app)


def get_dir_files():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(current_dir, 'samples', 'python_gpu')
    file_contents = {}

    for filename in os.listdir(target_dir):
        file_path = os.path.join(target_dir, filename)
        if os.path.isfile(file_path):
            with open(file_path, 'rb') as file:
                content = file.read()
            base64_content = base64.b64encode(content).decode('utf-8')
            file_contents[filename] = base64_content

    return file_contents


@pytest.mark.cuda
def test_python_gpu_basic():
    request = RunCodeRequest(language='python_gpu', code='', files=get_dir_files(), compile_timeout=90)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result.model_dump_json(indent=2))
    print('compile output:')
    print(result.compile_result.stdout)
    print('run output:')
    print(result.run_result.stdout)
    assert result.status == RunStatus.Success
    assert result.compile_result.status == CommandRunStatus.Finished
