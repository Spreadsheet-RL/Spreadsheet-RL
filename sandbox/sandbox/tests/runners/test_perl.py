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
def test_perl_print():
    request = RunCodeRequest(language='perl', code='''
print "Hello, World!\n";
    ''', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result)
    assert result.status == RunStatus.Success
    assert "Hello, World!" in result.run_result.stdout.strip()


@pytest.mark.minor
def test_perl_timeout():
    request = RunCodeRequest(language='perl',
                             code='''
sleep(5);
print("Finished waiting for 5 seconds.")
    ''',
                             run_timeout=1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


@pytest.mark.minor
def test_perl_assertion_success():
    request = RunCodeRequest(language='perl',
                             code='''
use Test::Deep;
use Data::Compare;

sub test {
        if(eq_deeply(1, 1)) {
        print "ok!" }else{
        exit 1;
        }
}

test();
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished


@pytest.mark.minor
def test_perl_assertion_error():
    request = RunCodeRequest(language='perl',
                             code='''
use Test::Deep;

sub test {
        if(eq_deeply(0, 1)) {
        print "ok!" }else{
        exit 1;
        }
}

test();
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
