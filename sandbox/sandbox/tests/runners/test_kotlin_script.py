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

from sandbox.runners import CommandRunStatus
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.server.server import app

client = TestClient(app)


@pytest.mark.minor
def test_kotlin_script_print():
    request = RunCodeRequest(language='kotlin_script', code='''
println("Hello, World!")
    ''', run_timeout=30)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert "Hello, World!" in result.run_result.stdout.strip()


@pytest.mark.minor
def test_kotlin_script_timeout():
    request = RunCodeRequest(language='kotlin_script',
                             code='''
fun main() {
    println("Starting...")
    
    // 让程序暂停 2 秒
    Thread.sleep(2000)
    
    println("Finished!")
}

main()
    ''',
                             run_timeout=1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


@pytest.mark.minor
def test_kotlin_script_assertion_success():
    request = RunCodeRequest(language='kotlin_script',
                             code='''
fun minCost() : Int {
    return 0
}

fun main() {
    var x : Int = minCost();
    var y : Int = 0;
    if (x != y) {
        throw Exception("Exception -- test case did not pass. x = " + x)
    }
}

main()
    ''',
                             run_timeout=40)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished


@pytest.mark.minor
def test_kotlin_script_assertion_error():
    request = RunCodeRequest(language='kotlin_script',
                             code='''
fun minCost() : Int {
    return 0
}

fun main() {
    var x : Int = minCost();
    var y : Int = 1;
    if (x != y) {
        throw Exception("Exception -- test case did not pass. x = " + x)
    }
}

main()
    ''',
                             run_timeout=20)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result)
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
    assert "java.lang.Exception" in result.run_result.stderr
