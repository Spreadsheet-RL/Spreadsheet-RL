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


def test_typescript_print():
    request = RunCodeRequest(language='typescript', code='console.log(123)', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.stdout.strip() == '123'


def test_typescript_timeout():
    request = RunCodeRequest(language='typescript',
                             code='''
    import util from 'util';

    const sleep = util.promisify(setTimeout);

    async function main() {
        await sleep(200);
    }

    main();
                             ''',
                             run_timeout=0.1)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.TimeLimitExceeded


def test_typescript_assertion_error():
    request = RunCodeRequest(language='typescript',
                             code='''
    import assert from "assert";
    assert.strictEqual(1, 2, '1 !== 2');
    ''')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.run_result.status == CommandRunStatus.Finished
    assert "AssertionError" in result.run_result.stderr


def test_typescript_humanevalx_assertion_error():
    request = RunCodeRequest(language='typescript', code='console.assert(1 === 2);')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    # ughhhh for humanevalx...
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert "Assertion failed" in result.run_result.stderr


def test_typescript_lodash_dep():
    request = RunCodeRequest(language='typescript',
                             code='''
    import _ from "lodash";
    import assert from "assert";

    assert(_.isEqual(1, 1));
    console.log('done');
                             ''',
                             run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    print(result)
    assert result.status == RunStatus.Success


def test_typescript_stdin():
    request = RunCodeRequest(language='typescript',
                             code='''
    process.stdin.on('data', (input: Buffer) => {
    const num: number = parseInt(input.toString());
    console.log(num);
    process.exit();
    });
                             ''',
                             run_timeout=5,
                             stdin='65535')
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.status == CommandRunStatus.Finished
    assert result.run_result.stdout == '65535\n'
