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

from sandbox.datasets.types import EvalResult, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)


async def test_live_code_bench_v1_get():
    request = GetPromptsRequest(dataset='live_code_bench_v1', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 3
    print(results)
    assert all(
        'You will be given a question (problem specification) and will generate a correct Python program that matches the specification and passes all tests.'
        in r.prompt for r in results)


async def test_live_code_bench_v1_get_fewshot():
    request = GetPromptsRequest(dataset='live_code_bench_v1', config=TestConfig(is_fewshot=True))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 3
    print(results)
    assert all('$1581.42' in r.prompt for r in results)


async def test_live_code_bench_v1_get_id():
    request = GetPromptByIdRequest(dataset='live_code_bench_v1', id=1, config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    assert 'You will be given a question (problem specification) and will generate a correct Python program that matches the specification and passes all tests.' in result.prompt
    assert 'Pick two cards, and swap them.' in result.prompt
    print(result.prompt)


async def test_live_code_bench_v1_list_ids():
    request = GetPromptsRequest(dataset='live_code_bench_v1', config=TestConfig())
    response = client.post('/list_ids', json=request.model_dump())
    assert response.status_code == 200
    result = response.json()
    assert result == IDS
    print(result)


IDS = [1, 10, 191]
PASSED_CODE = [
    ''' codeforce
```python
if __name__ == '__main__':
    for _ in range(int(input())):
        s = input()
        if s in ['abc', 'bac', 'cba', 'acb']:
            print('YES')
        else:
            print('NO')
```
    ''',
    ''' leetcode
```python
class Solution:
    def countSeniors(self, details):
        return len([x for x in details if int(x[11:13]) > 60])
```
    ''',
    ''' atcoder
```python
if __name__ == '__main__':
    n = int(input())
    s = input()
    cnt = s.count('T')
    if cnt * 2 > n or cnt * 2 == n and s[-1] == 'A':
        print('T')
    else:
        print('A')
```
    ''',
]

FAILED_CODE = [
    ''' codeforce
```python
if __name__ == '__main__':
    for _ in range(int(input())):
        s = input()
        if s in ['abc', 'bac', 'cba', 'acb']:
            print('NO')
        else:
            print('YES')
```
    ''',
    ''' leetcode
```python
class Solution:
    def countSeniors(self, details):
        return len([x for x in details if int(x[11:13]) <= 60])
```
    ''',
    ''' atcoder
```python
if __name__ == '__main__':
    n = int(input())
    s = input()
    cnt = s.count('T')
    if cnt * 2 > n or cnt * 2 == n and s[-1] == 'A':
        print('A')
    else:
        print('T')
```
    ''',
]


async def test_live_code_bench_v1_submit_passed():
    for i in range(3):
        request = SubmitRequest(
            dataset='live_code_bench_v1',
            id=IDS[i],
            config=TestConfig(language='python'),
            completion=PASSED_CODE[i],
        )
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        print(result.full_code)
        print('=' * 60)
        print(result.test_code)
        print('=' * 60)
        print(result.tests[0].exec_info)
        assert result.accepted is True


async def test_live_code_bench_v1_submit_failed():
    for i in range(3):
        request = SubmitRequest(
            dataset='live_code_bench_v1',
            id=IDS[i],
            config=TestConfig(language='python'),
            completion=FAILED_CODE[i],
        )
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        print(result.full_code)
        print('=' * 60)
        print(result.test_code)
        print('=' * 60)
        print(result.tests[0].exec_info)
        assert result.accepted is False
        assert 'Wrong Answer' in result.tests[0].exec_info.run_result.stdout
