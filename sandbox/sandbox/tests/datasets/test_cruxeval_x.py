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
import re
from fastapi.testclient import TestClient

from sandbox.utils.execution import max_concurrency
from sandbox.datasets.cruxeval import CruxEvalDataset
from sandbox.datasets.cruxeval import language_mappings
from sandbox.datasets.types import EvalResult, Prompt, TestConfig
from sandbox.server.online_judge_api import GetPromptByIdRequest, GetPromptsRequest, SubmitRequest
from sandbox.server.server import app

client = TestClient(app)

sample_ids = [
    "D_ut_0", "java_0", "python_0", "perl_0", "ruby_0", "lua_0", "rust_0", "julia_0", "bash_0", "scala_0", "racket_0",
    "swift_0", "csharp_0", "typescript_0", "R_0", "php_0", "cpp_0", "nodejs_0", "go_test_2"
]


def find_substr(A, B, rp):
    import re
    # 将A中的"????"替换成正则表达式中表示任意四个字符的部分 `(.*?)`
    pattern = re.escape(A).replace(rp, '(.*)')
    match = re.search(pattern, B, re.DOTALL)

    # 如果匹配成功，返回匹配的组
    return match.group(1)


async def test_cruxeval_x_get():
    request = GetPromptsRequest(dataset='cruxeval_x', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 38

    request = GetPromptsRequest(dataset='cruxeval_x', config=TestConfig(extra={'mode': 'input'}))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 19
    assert all(r.labels['mode'] == 'input' for r in results)
    print(results[1].prompt)

    request = GetPromptsRequest(dataset='cruxeval_x', config=TestConfig(extra={'mode': 'input'}))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 19
    assert all(r.labels['mode'] == 'input' for r in results)
    print(results[2].prompt)


async def test_cruxeval_x_get_id():
    request = GetPromptByIdRequest(dataset='cruxeval_x', id='java_0', config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    print(result)
    assert result.id == 'java_0'
    assert 'import java.io.*;\nimport java.lang.reflect.*;\n' in result.prompt


async def test_cruxeval_x_get_submit_passed():

    @max_concurrency(3)
    async def run_test(id):
        input, output, code, id = await CruxEvalDataset.get_test_info_by_id(id, 'cruxeval_x')
        lang = '_'.join(id.split('_')[:-1])
        input_case = find_substr(input, code, rp=re.escape(language_mappings[lang]))
        # output_case = find_substr(output, code, rp=r'\?\?\?\?')
        request = SubmitRequest(dataset='cruxeval_x',
                                id=id,
                                config=TestConfig(extra={'mode': 'input'}),
                                completion=f'''
to makeas  asjdena   
[ANSWER]
{input_case}
[/ANSWER]

[ANSWER]
{input_case}
[/ANSWER]

[ANSWER]
{input_case}
[/ANSWER]

[ANSWER]

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


async def test_cruxeval_x_get_submit_failed():

    @max_concurrency(3)
    async def run_test(id):
        input, output, code, id = await CruxEvalDataset.get_test_info_by_id(id, 'cruxeval_x')

        request = SubmitRequest(dataset='cruxeval_x',
                                id=id,
                                config=TestConfig(extra={'mode': 'output'}),
                                completion=f'''
[ANSWER]
f{output}
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
