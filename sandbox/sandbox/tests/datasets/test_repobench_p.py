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
from sandbox.datasets.repobench_p import RepobenchPDataset
from sandbox.datasets.types import EvalResult, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

from transformers import AutoTokenizer

client = TestClient(app)
ids = [1, 2, 3, 4, 5, 8]
seed_tokenizer = AutoTokenizer.from_pretrained("assets/tokenizer/gpt2")


async def test_repobench_p_python_get():
    request = GetPromptsRequest(dataset='repobench_p_python',
                                config=TestConfig(extra={
                                    'retrieval_mode': 'oracle-filled',
                                    'max_prompt_length': 8000
                                }))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]


async def test_repobench_p_java_get():
    request = GetPromptsRequest(dataset='repobench_p_java',
                                config=TestConfig(extra={
                                    'retrieval_mode': 'oracle-filled',
                                    'max_prompt_length': 8000,
                                }))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]


async def test_repobench_p_python_get_id():
    request = GetPromptByIdRequest(dataset='repobench_p_python',
                                   id=2,
                                   config=TestConfig(extra={
                                       'retrieval_mode': 'oracle-filled',
                                       'max_prompt_length': 8000,
                                   }))
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    assert result.id == 2
    # assert 'ls = list(sentence)\n    for letter in ls' in result.prompt


async def test_repobench_p_java_get_id():
    request = GetPromptByIdRequest(dataset='repobench_p_java',
                                   id=2,
                                   config=TestConfig(extra={
                                       'retrieval_mode': 'oracle-filled',
                                       'max_prompt_length': 8000,
                                   }))
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)
    assert result.id == 2


async def test_repobench_p_python_get_submit_passed():

    @max_concurrency(3)
    async def run_test(id):
        prompt, next_line = await RepobenchPDataset.get_test_info_by_id("repobench_p_python", id)
        request = SubmitRequest(dataset='repobench_p_python', id=id, config=TestConfig(), completion=f'{next_line}')

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    tasks = []
    for id in ids:
        tasks.append(run_test(id))

    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True


async def test_repobench_p_java_get_submit_passed():

    @max_concurrency(3)
    async def run_test(id):
        prompt, next_line = await RepobenchPDataset.get_test_info_by_id("repobench_p_java", id)
        request = SubmitRequest(dataset='repobench_p_java', id=id, config=TestConfig(), completion=f'{next_line}')

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    tasks = []
    for id in ids:
        tasks.append(run_test(id))

    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True


async def test_repobench_p_python_get_submit_failed():

    @max_concurrency(3)
    async def run_test(id):
        prompt, next_line = await RepobenchPDataset.get_test_info_by_id("repobench_p_python", id)
        request = SubmitRequest(dataset='repobench_p_python',
                                id=id,
                                config=TestConfig(),
                                completion=f"false answer{next_line}")

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    tasks = []
    for id in ids:
        tasks.append(run_test(id))
    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == False


async def test_repobench_p_java_get_submit_failed():

    @max_concurrency(3)
    async def run_test(id):
        prompt, next_line = await RepobenchPDataset.get_test_info_by_id("repobench_p_java", id)
        request = SubmitRequest(dataset='repobench_p_java',
                                id=id,
                                config=TestConfig(),
                                completion=f"false answer{next_line}")

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    tasks = []
    for id in ids:
        tasks.append(run_test(id))
    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == False


# asyncio.run(test_repobench_p_java_get())
# asyncio.run(test_repobench_p_python_get())
# asyncio.run(test_repobench_p_java_get_id())
# asyncio.run(test_repobench_p_python_get_id())
# asyncio.run(test_repobench_p_python_get_submit_passed())
# asyncio.run(test_repobench_p_java_get_submit_passed())
# asyncio.run(test_repobench_p_python_get_submit_failed())
# asyncio.run(test_repobench_p_java_get_submit_failed())
