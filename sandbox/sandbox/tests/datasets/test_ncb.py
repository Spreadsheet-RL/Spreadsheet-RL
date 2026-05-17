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

from sandbox.datasets.natural_code_bench import NaturalCodeBenchDataset
from sandbox.datasets.types import EvalResult, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)


async def test_ncb_get():
    request = GetPromptsRequest(dataset='ncb_java_zh', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    print(results)
    request = GetPromptsRequest(dataset='ncb_java_en', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    print(results)
    request = GetPromptsRequest(dataset='ncb_python_zh', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    print(results)
    request = GetPromptsRequest(dataset='ncb_python_en', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    print(results)


async def test_ncb_get_id():
    request = GetPromptByIdRequest(dataset='ncb_java_zh', id=4, config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)
    request = GetPromptByIdRequest(dataset='ncb_java_en', id=4, config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)
    request = GetPromptByIdRequest(dataset='ncb_python_zh', id=4, config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)
    request = GetPromptByIdRequest(dataset='ncb_python_en', id=4, config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)


async def test_ncb_submit_passed():
    for pid in range(2):
        print(f'java problem {pid}')
        request = SubmitRequest(dataset='ncb_java_zh',
                                id=pid,
                                config=TestConfig(),
                                completion=await NaturalCodeBenchDataset.get_canonical_solution('ncb_java_zh', pid))
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        if not result.accepted:
            print(result.full_code)
            print('===========================')
            print(result.tests[0].exec_info.compile_result.stdout)
            print('===========================')
            print(result.tests[0].exec_info.compile_result.stderr)
            print('===========================')
            print(result.tests[0].exec_info.run_result.stdout)
            print('===========================')
            print(result.tests[0].exec_info.run_result.stderr)
        assert result.accepted == True
        request = SubmitRequest(dataset='ncb_java_en',
                                id=pid,
                                config=TestConfig(),
                                completion=await NaturalCodeBenchDataset.get_canonical_solution('ncb_java_en', pid))
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True
    '''
    30: (deprecated since sklearn 0.2.7) TypeError: OneHotEncoder.__init__() got an unexpected keyword argument 'sparse' -> deprecated 不考虑
    56: 标准答案过不了
    '''
    for pid in range(2):
        print(f'python problem {pid}')
        request = SubmitRequest(dataset='ncb_python_zh',
                                id=pid,
                                config=TestConfig(),
                                completion=await NaturalCodeBenchDataset.get_canonical_solution('ncb_python_zh', pid))
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        if not result.accepted:
            print(result.full_code)
            print('===========================')
            print(result.tests[0].exec_info.run_result.stdout)
            print('===========================')
            print(result.tests[0].exec_info.run_result.stderr)

        assert result.accepted == True
        request = SubmitRequest(dataset='ncb_python_en',
                                id=pid,
                                config=TestConfig(),
                                completion=await NaturalCodeBenchDataset.get_canonical_solution('ncb_python_zh', pid))
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True
