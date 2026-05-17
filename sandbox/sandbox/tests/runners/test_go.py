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


def test_golang_print():
    request = RunCodeRequest(language='go',
                             code='''
    package main

    import (
        "fmt"
    )

    func main() {
        fmt.Println("123")
    }
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result)
    assert result.status == RunStatus.Success
    assert result.compile_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout.strip() == '123'


def test_golang_timeout():
    request = RunCodeRequest(language='go',
                             code='''
    package main

    import (
        "time"
    )

    func main() {
        time.Sleep(200 * time.Millisecond)
    }
    ''',
                             run_timeout=0.19,
                             compile_timeout=1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


def test_golang_stdin():
    request = RunCodeRequest(language='go',
                             code='''
    package main

    import "fmt"

    func main() {
        var num int
        fmt.Scan(&num)
        fmt.Println(num)
    }
    ''',
                             run_timeout=5,
                             stdin='65535')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout == '65535\n'
