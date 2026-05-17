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
from sandbox.datasets.mbxp import MBXPDataset
from sandbox.datasets.types import EvalResult, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)

SAMPLE_IDS = [
    84,  # 0, cpp
    85,  # 1, cpp
    97,  # 2, javascript
    111,  # 3, javascript
    1,  # 4, python
    2,  # 5, python
    77,  # 6, java
    80,  # 7, java
    78,  # 8, c
    79,  # 9, c
]


async def test_oodtest_v1_get():
    request = GetPromptsRequest(dataset='oodtest_v1_zh', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == len(SAMPLE_IDS)
    print(results)


async def test_oodtest_v1_get_id():
    request = GetPromptByIdRequest(dataset='oodtest_v1_zh', id=SAMPLE_IDS[0], config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    assert all(x not in result.prompt for x in ['问题：', '答案：'])
    print(result.prompt)


async def test_oodtest_v1_get_id_fewshot():
    request = GetPromptByIdRequest(dataset='oodtest_v1_zh', id=SAMPLE_IDS[0], config=TestConfig(is_fewshot=True))
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    assert all(x in result.prompt for x in ['问题：', '答案：'])
    print(result.prompt)


async def test_oodtest_v1_submit_passed():
    await MBXPDataset.get_test_info_by_id('oodtest_v1_zh', SAMPLE_IDS[0])

    @max_concurrency(3)
    async def run_test(id):
        code, programming_language = await MBXPDataset.get_test_info_by_id('oodtest_v1_zh', id)
        request = SubmitRequest(dataset='oodtest_v1_zh',
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

    tasks = []
    for id in SAMPLE_IDS:
        tasks.append(run_test(id))

    results = await asyncio.gather(*tasks)

    for response in results:
        # 题目没有配参考答案，就测下流程和 sandbox 正确性
        assert response.status_code == 200
        result = EvalResult(**response.json())
        print(result)
        assert result.extracted_type == 'fenced' and result.tests
        assert result.tests[0].exec_info.compile_result is not None or result.tests[0].exec_info.run_result is not None
