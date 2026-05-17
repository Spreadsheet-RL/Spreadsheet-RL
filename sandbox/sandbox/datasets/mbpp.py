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
from sandbox.utils.extraction import extract_code_from_freeform_completion
from sandbox.utils.sandbox_client import run_code_in_sandbox


def postprocess_completion(completion, stop_words=["\nassert", '\n"""']):
    if '[DONE]' in completion:
        completion = completion[:completion.index('[DONE]')]

    code, _ = extract_code_from_freeform_completion(completion, 'python', first_block_only=True)

    for st in stop_words:
        index = code.find(st)
        if index != -1:
            code = code[:index]
    return code


class MBPPDataset(CodingDataset):

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(request,
                                       cls.get_table_name(request.dataset),
                                       columns=['id', 'content', 'labels', 'test_list'])
        return [cls._generate_single_prompt(r, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'content', 'labels', 'test_list'])
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        ensure_json(row, 'labels')
        ensure_json(row, 'test_list')
        is_fewshot_task = config.is_fewshot
        if is_fewshot_task:
            task_id_2 = 'You are an expert Python programmer, and here is your task: Write a function to find the similar elements from the given two tuple lists. Your code should pass these tests:\n\nassert similar_elements((3, 4, 5, 6),(5, 7, 4, 10)) == (4, 5)\nassert similar_elements((1, 2, 3, 4),(5, 4, 3, 7)) == (3, 4)\nassert similar_elements((11, 12, 14, 13),(17, 15, 14, 13)) == (13, 14)\n[BEGIN]\ndef similar_elements(test_tup1, test_tup2):\r\n  res = tuple(set(test_tup1) & set(test_tup2))\r\n  return (res)\n[DONE]'
            task_id_3 = 'You are an expert Python programmer, and here is your task: Write a python function to identify non-prime numbers. Your code should pass these tests:\n\nassert is_not_prime(2) == False\nassert is_not_prime(10) == True\nassert is_not_prime(35) == True\n[BEGIN]\nimport math\r\ndef is_not_prime(n):\r\n    result = False\r\n    for i in range(2,int(math.sqrt(n)) + 1):\r\n        if n % i == 0:\r\n            result = True\r\n    return result\n[DONE]'
            task_id_4 = 'You are an expert Python programmer, and here is your task: Write a function to find the largest integers from a given list of numbers using heap queue algorithm. Your code should pass these tests:\n\nassert heap_queue_largest( [25, 35, 22, 85, 14, 65, 75, 22, 58],3)==[85, 75, 65] \nassert heap_queue_largest( [25, 35, 22, 85, 14, 65, 75, 22, 58],2)==[85, 75]\nassert heap_queue_largest( [25, 35, 22, 85, 14, 65, 75, 22, 58],5)==[85, 75, 65, 58, 35]\n[BEGIN]\nimport heapq as hq\r\ndef heap_queue_largest(nums,n):\r\n  largest_nums = hq.nlargest(n, nums)\r\n  return largest_nums\n[DONE]'
            task = f"You are an expert Python programmer, and here is your task: {row['content']} Your code should pass these tests:\n\n{row['test_list'][0]}\n{row['test_list'][1]}\n[BEGIN]\n"
            prompt = task_id_2 + '\n' + task_id_3 + '\n' + task_id_4 + '\n' + task
        else:
            tests = '\n'.join(row['test_list'])
            prompt = f"You are an expert Python programmer, and here is your task: {row['content']} Your code should pass these tests:\n\n{tests}"

        return Prompt(id=row['id'], prompt=prompt, labels=row['labels'])

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'content', 'labels', 'test_list'])
        ensure_json(row, 'labels')
        ensure_json(row, 'test_list')

        text = request.completion
        completion = postprocess_completion(text)
        code = '\n'.join([row['labels']['test_setup_code'], completion])
        if 'if __name__ ==' in code:
            code = code[:code.index('if __name__ ==')]

        is_fewshot_task = request.config.is_fewshot
        full_code = code
        if is_fewshot_task:
            full_code += '\n' + row['test_list'][-1] + '\n'
        else:
            for test in row['test_list']:
                full_code += '\n' + test + '\n'

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
                          tests=[EvalTestCase(
                              passed=accepted,
                              exec_info=result,
                          )])
