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

from fastapi.testclient import TestClient

from sandbox.runners import CommandRunStatus
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.server.server import app

client = TestClient(app)


def test_bash_echo():
    request = RunCodeRequest(language='bash', code='echo "Hello World"', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.stdout.strip() == 'Hello World'


def test_bash_sleep_timeout():
    request = RunCodeRequest(language='bash', code='sleep 0.2', run_timeout=0.1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


def test_bash_false_condition():
    request = RunCodeRequest(language='bash', code='test 1 -eq 2', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    # Note: Bash scripts return 0 on success and a non-zero exit code on failure.
    # This test assumes a non-zero exit code indicates a "Failed" status in your framework.
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished


def test_bash_syntax_error():
    request = RunCodeRequest(language='bash', code='if [', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
    assert 'syntax error' in result.run_result.stderr


def test_bash_file_read():
    request = RunCodeRequest(language='bash',
                             code='cat dir1/dir2/dir3/secret_flag',
                             run_timeout=5,
                             files={'dir1/dir2/dir3/secret_flag': "ImhlbGxvLCB0aGlzIGlzIGEgdGVzdCI="})
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert 'hello, this is a test' in result.run_result.stdout


def test_bash_stdin():
    request = RunCodeRequest(language='bash',
                             code='''
    read input
    echo $input
    ''',
                             run_timeout=5,
                             stdin='65535')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout == '65535\n'
