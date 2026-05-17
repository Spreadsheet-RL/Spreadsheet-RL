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

import asyncio

from fastapi.testclient import TestClient

from sandbox.utils.execution import max_concurrency
from sandbox.datasets.cruxeval import CruxEvalDataset
from sandbox.datasets.types import EvalResult, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)

sample_ids = ['sample_0', 'sample_1', 'sample_10', 'sample_100']


async def test_cruxeval_get():
    request = GetPromptsRequest(dataset='cruxeval', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 40

    request = GetPromptsRequest(dataset='cruxeval', config=TestConfig(extra={'mode': 'input'}))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 20
    assert all(r.labels['mode'] == 'input' for r in results)
    print(results[0])

    request = GetPromptsRequest(dataset='cruxeval', config=TestConfig(extra={'mode': 'output'}))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 20
    assert all(r.labels['mode'] == 'output' for r in results)
    print(results[0])

    request = GetPromptsRequest(dataset='cruxeval', config=TestConfig(extra={'use_cot': True}))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert all('[THOUGHT]' in r.prompt for r in results)

    request = GetPromptsRequest(dataset='cruxeval', config=TestConfig(extra={'phind_output': True, 'mode': 'output'}))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert all('# done' in r.prompt for r in results)


async def test_cruxeval_get_id():
    request = GetPromptByIdRequest(dataset='cruxeval', id='sample_112', config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)
    assert result.id == 'sample_112'
    assert 'ls = list(sentence)\n    for letter in ls' in result.prompt


async def test_cruxeval_get_submit_passed():

    @max_concurrency(3)
    async def run_test(id):
        input, output = await CruxEvalDataset.get_test_info_by_id(id)
        request = SubmitRequest(dataset='cruxeval',
                                id=id,
                                config=TestConfig(extra={'mode': 'output'}),
                                completion=f'''
[ANSWER]
assert f({input}) == {output}
[/ANSWER]

[PYTHON]
def f(my_list):
    return my_list[0]
assert f(??) == 1
[/PYTHON]
[ANSWER]
assert f([1, 2, 3]) == 1
[/ANSWER]

[PYTHON]
def f(my_list):
''')

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    tasks = []
    for id in sample_ids:
        tasks.append(run_test(id))

    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True


async def test_cruxeval_get_submit_failed():

    @max_concurrency(3)
    async def run_test(id):
        input, output = await CruxEvalDataset.get_test_info_by_id(id)
        request = SubmitRequest(dataset='cruxeval',
                                id=id,
                                config=TestConfig(extra={'mode': 'output'}),
                                completion=f'''
[ANSWER]
assert f({input}) == {output}+1
[/ANSWER]
''')

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    tasks = []
    for id in sample_ids:
        tasks.append(run_test(id))
    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == False
