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

import json
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from sandbox.database import get_row_by_id_in_table, get_rows_in_table
from sandbox.datasets.types import (
    CodingDataset,
    EvalResult,
    EvalTestCase,
    GetPromptByIdRequest,
    GetPromptsRequest,
    Prompt,
    RunCodeRequest,
    RunCodeResponse,
    RunStatus,
    SubmitRequest,
    TestConfig,
)
from sandbox.utils.antihack import antis
from sandbox.utils.common import ensure_json
from sandbox.utils.extraction import extract_code_from_freeform_completion_v2
from sandbox.utils.sandbox_client import run_code_in_sandbox


class ExtractCodeMode(Enum):
    """ Extract code mode.
    """
    FIRST_BLOCK_ONLY = 'first'
    MERGE_ALL_BLOCKS = 'all'

    @classmethod
    def is_valid(cls, value) -> bool:
        return value in [e.value for e in cls]


def append_test(code: str, test: str):
    insert_token = '#<INSERT>'

    if insert_token is None:
        if 'if __name__ ==' in code:
            code = code[:code.index('if __name__ ==')]
        full_code = ''.join([code, '\n', test])
    else:
        parts = test.split(insert_token)
        full_code = ''.join([parts[0], code, parts[1]])
    return full_code


class MBXPDataset(CodingDataset):

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content'],
        )
        for row in rows:
            ensure_json(row, 'labels')
        prompt_language = 'en' if '_en' in request.dataset else 'zh'
        return [cls._generate_single_prompt(r, request.config, prompt_language) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'labels', 'content'])
        ensure_json(row, 'labels')
        prompt_language = 'en' if '_en' in request.dataset else 'zh'
        return cls._generate_single_prompt(row, request.config, prompt_language)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig, prompt_language) -> Prompt:
        question = row['content']
        context = row['labels'].get('docs')  # str | None
        task_id = row['labels']['task_id']

        autoeval_wrap_prompt = config.extra.get('autoeval_wrap_prompt') == True

        if config.is_fewshot:
            autoeval_custom_fewshot = config.extra.get('autoeval_custom_fewshot', {})
            assert isinstance(autoeval_custom_fewshot, dict)
            task_type = task_id.split('/')[0]
            if task_type in autoeval_custom_fewshot:
                fewshot = autoeval_custom_fewshot[task_type]
            elif 'default' in autoeval_custom_fewshot:
                fewshot = autoeval_custom_fewshot['default']
            elif 'fewshot' in row['labels']:
                fewshot = row['labels']['fewshot']
            else:
                raise ValueError(f"Missing fewshot of {task_id}")
        else:
            fewshot = None

        prompt = cls._build_prompt(question, fewshot, context, autoeval_wrap_prompt, prompt_language)
        return Prompt(id=row['id'], prompt=prompt, labels=row['labels'])

    @staticmethod
    def _build_prompt(question: str, fewshot: Optional[str], context: Optional[str], autoeval_wrap_prompt: bool,
                      prompt_language: Literal['zh', 'en']) -> str:
        # Compose from top to bottom: context, fewshot, question
        # 这里分4种情况：1）有 context 且开 fewshot；2）只有 context；3）只开 fewshot；4）无 context 也不开 fewshot；
        if prompt_language == 'zh':
            q_hint = '问题：'
            a_hint = '答案：'
            wrap_prompt_ins = '请用中文回答，并用Markdown格式。####Instruction:'
            wrap_prompt_res = 'Response:'
        elif prompt_language == 'en':
            q_hint = 'Question:'
            a_hint = 'Answer:'
            wrap_prompt_ins = 'Please answer in English with Markdown format. ####Instruction:'
            wrap_prompt_res = 'Response:'

        sep = '----------------'
        if context and fewshot:
            prompt = f'{context}\n\n{sep}\n\n{fewshot}\n\n{q_hint}{question}\n\n{a_hint}\n'
        elif context:
            if autoeval_wrap_prompt:
                prompt = f'{context}\n\n{wrap_prompt_ins}\n{question}\n{wrap_prompt_res}\n'
            else:
                prompt = f'{context}\n\n{q_hint}{question}\n\n{a_hint}\n'
        elif fewshot:
            prompt = f'{fewshot}\n\n{q_hint}{question}\n\n{a_hint}\n'
        else:
            if autoeval_wrap_prompt:
                prompt = f'{wrap_prompt_ins}\n{question}\n{wrap_prompt_res}\n'
            else:
                prompt = question
        return prompt

    @classmethod
    async def get_test_info_by_id(cls, table_name: Literal['mbxp_v1_en', 'humanevalds_v1_en'], id: int) -> str:
        """ This function only used for unittest case.
        """
        row = await get_row_by_id_in_table(GetPromptByIdRequest(dataset=table_name, config=TestConfig(), id=id),
                                           cls.get_table_name(table_name),
                                           columns=['canonical_solution', 'labels'])
        ensure_json(row, 'labels')
        return row['canonical_solution'], row['labels']['programming_language']

    @staticmethod
    def _param_inner_function_only(dataset_name: str, programming_language: str) -> bool:
        flag = False
        if programming_language in ['csharp']:
            flag = True
        elif programming_language in ['java']:
            if dataset_name.startswith('humanevalds'):
                flag = True
        return flag

    @staticmethod
    def _post_judge(language: str, extracted_code: str, result: RunCodeResponse) -> Optional[bool]:
        """ Do some post judge with extracted code and sandbox result.
        Returns:
        - If return None, means this post judge is skipped, ignore this result.
        - Otherwise use this return value for final `accepted` status.
        """
        if language == 'racket':
            # humaneval racket 里面测试代码有个特性，当 test-case 失败的时候进程 exit_code=0，因此需要去检查 stderr 内容
            if result.run_result and result.run_result.stderr:
                stderr = result.run_result.stderr
                if 'FAILURE' in stderr:
                    return False
        return None

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(request, cls.get_table_name(request.dataset), columns=['test', 'labels'])
        ensure_json(row, 'labels')
        ensure_json(row, 'test')
        asset = row['test'].get('asset')
        if isinstance(asset, dict):
            asset = asset
        elif isinstance(asset, str):
            asset = json.loads(asset)
        else:
            asset = {}
        asset = {k: v for k, v in asset.items() if v is not None}
        extract_code_mode = request.config.extra.get('autoeval_extract_code_mode',
                                                     ExtractCodeMode.FIRST_BLOCK_ONLY.value)
        assert ExtractCodeMode.is_valid(extract_code_mode), f'Invalid autoeval_extract_code_mode: {extract_code_mode}'
        first_block_only = extract_code_mode == ExtractCodeMode.FIRST_BLOCK_ONLY.value
        programming_language = row['labels']['programming_language']
        exactly_match = first_block_only
        inner_function_only = cls._param_inner_function_only(request.dataset, programming_language)
        code, extracted_type = extract_code_from_freeform_completion_v2(request.completion,
                                                                        programming_language,
                                                                        first_block_only,
                                                                        no_removal=True,
                                                                        exactly_match=exactly_match,
                                                                        inner_function_only=inner_function_only)
        full_code = append_test(code, row['test']['code'])
        language = row['labels']['execution_language']

        result = await run_code_in_sandbox(
            RunCodeRequest(
                code=full_code,
                language=language,
                compile_timeout=30,
                run_timeout=30,
                files=asset,
            ))
        accepted = result.status == RunStatus.Success

        # anti-hack
        if language in antis:
            accepted = accepted and antis[language].judge(code)

        # post judge
        post_accepted = cls._post_judge(programming_language, code, result)
        if post_accepted in [True, False]:
            accepted = post_accepted

        return EvalResult(id=request.id,
                          accepted=accepted,
                          extracted_code=code,
                          full_code=full_code,
                          test_code=row['test']['code'],
                          tests=[EvalTestCase(
                              passed=accepted,
                              exec_info=result,
                          )],
                          extracted_type=extracted_type)
