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

import base64
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


class AiderBenchmarkDataset(CodingDataset):

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content'],
        )
        return [cls._generate_single_prompt(r, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content'],
        )
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        wrap_prompt_ins = 'Please answer in English with Markdown format. Below is the problem:'
        wrap_prompt_res = 'Your response here:'

        question = row['content']
        labels = ensure_json(row, 'labels')
        reference = labels['reference']
        prompt = f"""
{question}

Please generate the code in the following format:
```python
{reference}
```
"""

        autoeval_wrap_prompt = config.extra.get('autoeval_wrap_prompt')
        if autoeval_wrap_prompt:
            prompt = f'{wrap_prompt_ins}\n{prompt}\n{wrap_prompt_res}\n'

        return Prompt(id=row['id'], prompt=prompt, labels=labels)

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content', 'test'],
        )
        test = ensure_json(row, 'test')
        test_code = test['code']
        asset = test['asset']
        real_test_code = [base64.b64decode(x).decode('utf-8') for x in asset.values()]

        code = default_extract_helper(request.completion, 'python', request.config.custom_extract_logic)
        full_code = test_code.replace('#<INSERT>', code)

        result = await run_code_in_sandbox(
            RunCodeRequest(
                code=full_code,
                language='python',
                run_timeout=request.config.run_timeout or 60,
                files=asset,
            ))
        accepted = result.status == RunStatus.Success

        return EvalResult(
            id=request.id,
            accepted=accepted,
            extracted_code=code,
            full_code=full_code,
            test_code='\n\n'.join(real_test_code),
            tests=[EvalTestCase(passed=accepted, exec_info=result)],
        )
