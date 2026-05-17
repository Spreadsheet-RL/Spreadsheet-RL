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

from fastapi.testclient import TestClient

from sandbox.runners import CommandRunStatus
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.server.server import app

client = TestClient(app)


def test_python_print():
    request = RunCodeRequest(language='python', code='print(123)', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.stdout.strip() == '123'


def test_python_timeout():
    request = RunCodeRequest(language='python', code='import time; time.sleep(0.2)', run_timeout=0.1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


def test_python_assertion_error():
    request = RunCodeRequest(language='python', code='assert 1 == 2', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
    assert 'AssertionError' in result.run_result.stderr


def test_python_syntax_error():
    request = RunCodeRequest(language='python', code='int a = 1', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
    assert 'SyntaxError: invalid syntax' in result.run_result.stderr


def test_python_file_read():
    request = RunCodeRequest(language='python',
                             code='print(open("dir1/dir2/dir3/secret_flag").read())',
                             run_timeout=5,
                             files={'dir1/dir2/dir3/secret_flag': "ImhlbGxvLCB0aGlzIGlzIGEgdGVzdCI="})
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert 'hello, this is a test' in result.run_result.stdout


def test_python_stdin():
    request = RunCodeRequest(language='python', code='print(int(input()))', run_timeout=5, stdin='65535')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout == '65535\n'


def test_python_fetch_files():
    request = RunCodeRequest(language='python',
                             code='open("a.txt", "w").write("secret"); open("/mnt/b", "w").write("sauce");',
                             run_timeout=5,
                             fetch_files=['a.txt', '/mnt/b'])
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert base64.b64decode(result.files['a.txt'].encode()).decode() == 'secret'
    assert base64.b64decode(result.files['/mnt/b'].encode()).decode() == 'sauce'
