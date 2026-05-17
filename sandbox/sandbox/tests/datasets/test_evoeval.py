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

from sandbox.datasets.types import EvalResult, EvalTestCase, Prompt, RunCodeResponse, RunStatus, TestConfig
from sandbox.server.online_judge_api import (
    GetMetricsFunctionRequest,
    GetMetricsFunctionResult,
    GetPromptByIdRequest,
    GetPromptsRequest,
    SubmitRequest,
)
from sandbox.server.server import app

client = TestClient(app)


class utils_coding:

    @staticmethod
    def pass_at_k_v2(samples, n: int, k: int) -> float:
        import numpy

        def codex_estimator(n: int, c: int, k: int) -> float:
            """
            Calculates 1 - comb(n - c, k) / comb(n, k).
            """
            if n - c < k:
                return 1.0
            return 1.0 - numpy.prod(1.0 - k / numpy.arange(n - c + 1, n + 1))

        from collections import defaultdict
        """ Compute Pass@k metric.
            Args:
                samples: list of (task_name/id, passed) pair
                n: total sample times
            Returns:
                final average Pass@k score
        """
        correct_dict = defaultdict(int)
        for name, passed in samples:
            if passed:
                correct_dict[name] += 1
            else:
                correct_dict[name] += 0

        final_scores = []
        for _, c in correct_dict.items():
            score = codex_estimator(n, c, k)
            final_scores.append(score)
        if final_scores:
            final_score = sum(final_scores) / len(final_scores)
        else:
            final_score = 0.0  # empty case

        return final_score


async def test_evoeval_get():
    request = GetPromptsRequest(dataset='evoeval', config=TestConfig())
    response = client.post('/get_prompts', json=request.model_dump())
    assert response.status_code == 200
    results = [Prompt(**sample) for sample in response.json()]
    # print(results)
    assert len(results) == 11


async def test_evoeval_get_id():
    request = GetPromptByIdRequest(dataset='evoeval', id='EvoEval_combine_EvoEval/85', config=TestConfig())
    response = client.post('/get_prompt_by_id', json=request.model_dump())
    assert response.status_code == 200
    result = Prompt(**response.json())
    # print(result)


async def test_evoeval_list_ids():
    request = GetPromptsRequest(dataset='evoeval', config=TestConfig())
    response = client.post('/list_ids', json=request.model_dump())
    assert response.status_code == 200
    # print(response.json())


async def test_evoeval_submit_passed():
    request = SubmitRequest(dataset='evoeval',
                            id='EvoEval_combine_EvoEval/85',
                            config=TestConfig(language='python'),
                            completion='''
    sorted_list = sorted(lst, key=lambda x: (bin(x).count('1'), x))
    
    for x in sorted_list:
        if x != sorted_list[0]:
            return x 
    return None
def next_smallest_sorted_binary(lst): 
    sorted_list = sorted(lst, key=lambda x: (bin(x).count('1'), x))
    
    for x in sorted_list:
        if x != sorted_list[0]:
            return x 
    ret
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == True


async def test_evoeval_submit_passed_freeform():
    request = SubmitRequest(dataset='evoeval',
                            id='EvoEval_combine_EvoEval/85',
                            config=TestConfig(language='python', extra={'is_freeform': True}),
                            completion='''
Here is the answer:
```python
def next_smallest_sorted_binary(lst): 
    sorted_list = sorted(lst, key=lambda x: (bin(x).count('1'), x))
    
    for x in sorted_list:
        if x != sorted_list[0]:
            return x 
    return None
```
''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == True


async def test_evoeval_submit_failed():
    request = SubmitRequest(dataset='evoeval',
                            id='EvoEval_combine_EvoEval/85',
                            config=TestConfig(language='python'),
                            completion='''
    sorted_list = sorted(lst, key=lambda x: (bin(x).count('1'), x))
    
    for x in sorted_list:
        if x != sorted_list[0]:
            return 1

''')
    response = client.post('/submit', json=request.model_dump())
    assert response.status_code == 200
    result = EvalResult(**response.json())
    assert result.accepted == False


async def test_evoeval_get_metric_function():
    request = GetMetricsFunctionRequest(dataset='evoeval', config=TestConfig())
    response = client.post('/get_metrics_function', json=request.model_dump())
    result = GetMetricsFunctionResult(**response.json())
    repeats = 1
    k_targets = [1, 10]
    ctx = {'repeats': repeats, 'k_targets': k_targets, 'utils_coding': utils_coding}
    exec(result.function, ctx, ctx)
    get_metrics = ctx['get_metrics']
    test_es = EvalResult(id='EvoEval_combine_EvoEval/85',
                         accepted=True,
                         extracted_code='None',
                         full_code='',
                         tests=[
                             EvalTestCase(passed=1,
                                          exec_info=RunCodeResponse(status=RunStatus('Success'), message=''),
                                          test_info={})
                         ])
    print(get_metrics([test_es]))
