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
from typing import List, Literal, Optional

from fastapi.testclient import TestClient
from pydantic import BaseModel

from sandbox.utils.execution import max_concurrency
from sandbox.datasets.mbxp import MBXPDataset
from sandbox.datasets.types import EvalResult, EvalTestCase, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)

SAMPLE_IDS = [
    1,  # 0, python
    2,  # 1, python
    465,  # 2, cpp
    466,  # 3, cpp
    1314,  # 4, java
    1315,  # 5, java
    1772,  # 6, typescript
    1773,  # 7, typescript
    2230,  # 8, typescript
    2231,  # 9, typescript
    2692,  # 10, kotlin_script
    2693,  # 11, kotlin_script
    3148,  # 12, ruby
    3149,  # 13, ruby
    3606,  # 14, php
    3607,  # 15, php
    4064,  # 16, perl
    4065,  # 17, perl
    4522,  # 18, scala
    4523,  # 19, scala
]


class OldEvalResult(BaseModel):
    id: int | str
    accepted: bool
    extracted_code: str
    full_code: Optional[str] = None
    tests: List[EvalTestCase]
    extracted_type: Optional[Literal['fenced', 'incomplete_fenced', 'heuristic', 'empty']] = None


async def test_mbxp_v1_get():
    request = GetPromptsRequest(dataset='mbxp_v1_en', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == len(SAMPLE_IDS)
    print(results)
    assert all('To complete the following code tasks' in r.prompt for r in results)


async def test_mbxp_v1_get_id():
    request = GetPromptByIdRequest(dataset='mbxp_v1_en', id=SAMPLE_IDS[0], config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    assert 'Question:To complete the following code tasks, write the full-code' not in result.prompt
    print(result.prompt)


async def test_mbxp_v1_get_id_fewshot():
    request = GetPromptByIdRequest(dataset='mbxp_v1_en', id=SAMPLE_IDS[0], config=TestConfig(is_fewshot=True))
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    assert 'Question:To complete the following code tasks, write the full-code' in result.prompt
    print(result.prompt)


async def test_mbxp_v1_submit_passed():
    await MBXPDataset.get_test_info_by_id('mbxp_v1_en', SAMPLE_IDS[0])

    @max_concurrency(3)
    async def run_test(id):
        code, programming_language = await MBXPDataset.get_test_info_by_id('mbxp_v1_en', id)
        request = SubmitRequest(dataset='mbxp_v1_en',
                                id=id,
                                config=TestConfig(),
                                completion=f'''
```{programming_language}
{code}
```
    ''')

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    id_to_lang = {}
    tasks = []
    for id in SAMPLE_IDS:
        tasks.append(run_test(id))
        _, programming_language = await MBXPDataset.get_test_info_by_id('mbxp_v1_en', id)
        id_to_lang[id] = programming_language

    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        _ = OldEvalResult(**response.json())  # compatible with seed/evals
        result = EvalResult(**response.json())
        assert result.test_code is not None
        if id_to_lang[result.id] in ['cpp']:
            assert result.tests[0].exec_info.run_result.return_code == 0
        else:
            assert result.accepted == True
            if not result.accepted:
                print(result.full_code)


async def test_mbxp_v1_submit_failed():
    code, programming_language = await MBXPDataset.get_test_info_by_id('mbxp_v1_en', SAMPLE_IDS[0])
    request = SubmitRequest(dataset='mbxp_v1_en',
                            id=SAMPLE_IDS[1],
                            config=TestConfig(),
                            completion=f'''
```{programming_language}
{code}
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == False


# ------ v2 ------ #


def get_data_v2():
    request = GetPromptsRequest(dataset='mbxp_v2_en', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    return results


async def test_mbxp_v2_get():
    results = get_data_v2()
    assert len(results) > 0
    print(results)
    assert all('To complete the following code tasks' in r.prompt for r in results)


async def test_mbxp_v2_submit_passed():
    dataset_name = 'mbxp_v2_en'
    samples = get_data_v2()
    sample_ids = [sample.id for sample in samples if sample.id < 10000]

    @max_concurrency(3)
    async def run_test(dataset_name, id):
        code, programming_language = await MBXPDataset.get_test_info_by_id(dataset_name, id)
        request = SubmitRequest(dataset=dataset_name,
                                id=id,
                                config=TestConfig(),
                                completion=f'''
```{programming_language}
{code}
```
    ''')

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    id_to_lang = {}
    tasks = []
    for id in sample_ids:
        tasks.append(run_test(dataset_name, id))
        _, programming_language = await MBXPDataset.get_test_info_by_id(dataset_name, id)
        id_to_lang[id] = programming_language
    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True


async def test_mbxp_v2_submit_failed():
    dataset_name = 'mbxp_v2_en'
    samples = get_data_v2()
    sample_ids = [sample.id for sample in samples if sample.id >= 10000]

    @max_concurrency(3)
    async def run_test(dataset_name, id):
        code, programming_language = await MBXPDataset.get_test_info_by_id(dataset_name, id)
        request = SubmitRequest(dataset=dataset_name,
                                id=id,
                                config=TestConfig(),
                                completion=f'''
```{programming_language}
{code}
```
    ''')

        def post():
            return client.post('/submit', json=request.model_dump())

        response = await asyncio.to_thread(post)
        return response

    id_to_lang = {}
    tasks = []
    for id in sample_ids:
        tasks.append(run_test(dataset_name, id))
        _, programming_language = await MBXPDataset.get_test_info_by_id(dataset_name, id)
        id_to_lang[id] = programming_language
    results = await asyncio.gather(*tasks)

    for response in results:
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == False
