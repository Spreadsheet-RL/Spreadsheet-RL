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
from sandbox.server.online_judge_api import GetPromptsRequest, SubmitRequest
from sandbox.server.server import app
from sandbox.utils.common import load_jsonl
import os

client = TestClient(app)

samples_path = os.path.join(os.path.dirname(__file__), 'samples', 'Multi-PLE', 'open_humaneval_python.jsonl')


async def test_humaneval_python_get():
    samples = load_jsonl(samples_path)
    request = GetPromptsRequest(dataset='humaneval_python',
                                config=TestConfig(dataset_type='HumanEvalDataset', provided_data=samples))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 164
    print(results[0].labels)

    request = GetPromptsRequest(dataset='humaneval_python',
                                config=TestConfig(dataset_type='HumanEvalDataset',
                                                  provided_data=samples,
                                                  extra={'is_freeform': True}))
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    assert len(results) == 164
    print(results[0].prompt)


async def test_humaneval_python_submit_passed():
    samples = load_jsonl(samples_path)
    for sample in samples[:10]:
        solution = sample['canonical_solution']
        request = SubmitRequest(dataset='humaneval_python',
                                id=0,
                                config=TestConfig(dataset_type='HumanEvalDataset',
                                                  language='python',
                                                  provided_data=sample),
                                completion=solution + '\n\ndef random')
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True

        prompt = sample['prompt']
        request = SubmitRequest(dataset='humaneval_python',
                                id=0,
                                config=TestConfig(dataset_type='HumanEvalDataset',
                                                  language='python',
                                                  provided_data=sample,
                                                  extra={'is_freeform': True}),
                                completion='Here is your answer:\n'
                                '```python\n' + prompt + '\n' + solution + '\n```')
        response = client.post('/submit', json=request.model_dump())
        assert response.status_code == 200
        result = EvalResult(**response.json())
        assert result.accepted == True


async def test_humaneval_python_submit_failed():
    samples = load_jsonl(samples_path)
    sample = samples[0]
    request = SubmitRequest(dataset='humaneval_python',
                            id=0,
                            config=TestConfig(dataset_type='HumanEvalDataset', language='python', provided_data=sample),
                            completion='''
    mean = max(numbers) / len(numbers)
    return sum(abs(x - mean) for x in numbers) / len(numbers)
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == False
