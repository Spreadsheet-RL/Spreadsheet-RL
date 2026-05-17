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

import inspect
from typing import Any, Dict, List

from sandbox.database import get_row_by_id_in_table, get_rows_in_table
from sandbox.datasets.types import (
    CodingDataset,
    EvalResult,
    EvalTestCase,
    GetPromptByIdRequest,
    GetPromptsRequest,
    Prompt,
    RunCodeRequest,
    RunStatus,
    SubmitRequest,
    TestConfig,
)
from sandbox.utils.common import ensure_json
from sandbox.utils.extraction import default_extract_helper
from sandbox.utils.sandbox_client import run_code_in_sandbox

types = ['Distraction', 'Redefinition', 'Shortcut', 'Commonsense', 'Cornercase', 'Complex', 'Codesense']


def get_categories(results):
    from collections import defaultdict
    categories_counts = defaultdict(list)
    for r in results:
        cate = r.tests[0].test_info['difficulty_type']
        categories_counts[cate].append(r)

    return categories_counts


class MHPPDataset(CodingDataset):

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content', 'test'],
        )
        return [cls._generate_single_prompt(r, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'labels', 'content', 'test'])
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        tests = row['test']
        test = tests.split('\n')[0]
        if config.extra.get('pretrain_mode'):
            prompt = default_extract_helper(row['content'], 'python')
        else:
            prompt = row['content']
        prompt = prompt[:prompt.rfind('"""')]
        prompt = f'{prompt}\n    e.g. {test} """'
        return Prompt(id=row['id'], prompt=prompt, labels=ensure_json(row, 'labels'))

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'content', 'test', 'labels'])
        ensure_json(row, 'labels')
        test = row['test']

        code = default_extract_helper(request.completion, 'python', request.config.custom_extract_logic)
        if not code.strip():
            code = default_extract_helper(row['content'] + '\n' + request.completion, 'python',
                                          request.config.custom_extract_logic)
        full_code = f"{code}\n{test}"
        result = await run_code_in_sandbox(
            RunCodeRequest(
                code=full_code,
                language='python',
                run_timeout=request.config.run_timeout or 20,
            ))
        accepted = result.status == RunStatus.Success

        return EvalResult(id=request.id,
                          accepted=accepted,
                          extracted_code=code,
                          full_code=full_code,
                          tests=[EvalTestCase(passed=accepted, exec_info=result, test_info=row['labels'])])

    @classmethod
    def get_metrics_function(cls) -> str:
        source = f'''
{inspect.getsource(get_categories)}

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

def get_metrics(results):
    categories = get_categories(results)
    performance = {{}}
    for cat, samples in categories.items():
        for k in k_targets:
            if repeats < k:
                continue
            pak = pass_at_k_v2([(s.id, s.accepted) for s in samples], repeats, k)
            performance[f'{{cat}}/Pass@k={{k}}'] = pak
    return performance
'''
        return source
