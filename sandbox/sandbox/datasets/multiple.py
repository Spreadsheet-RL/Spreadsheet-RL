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

import re
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
from sandbox.utils.extraction import default_extract_helper, extract_code_from_freeform_completion
from sandbox.utils.helpers import END_TOKENS, IMPORT_HELPER
from sandbox.utils.sandbox_client import run_code_in_sandbox


def stop_token_trim(s: str, row: Dict[str, Any], excepts: List[str] = ['\n\n']) -> str:
    stop_tokens = row.get('stop_tokens')
    if stop_tokens:
        for st in stop_tokens:
            if st in excepts:
                continue
            if st.startswith('re:'):
                # use regex to find the stop token
                pattern = re.compile(st[3:].strip())
                match = pattern.search(s)
                if match:
                    s = s[:match.start()]
            else:
                index = s.find(st)
                if index != -1:
                    s = s[:index]
    return s


def stop_after_stop_token(s: str, language: str) -> str:
    if end_tokens := END_TOKENS.get(language):
        for et in end_tokens:
            index = s.find(et)
            if index != -1:
                s = s[:index] + et
    return s


def postprocess_full_code(code, language):
    if language == 'go':
        packages = set(re.findall(r'package\s+(\w+)', code))
        code = re.sub(r'package\s+(\w+)', '', code)
        # combine all imports
        single_imports = re.findall(r'import\s+("\w+")', code)
        multi_imports = sum([x.split('\n') for x in re.findall(r'import\s+\((.*?)\)', code, flags=re.DOTALL)], [])
        multi_imports = [x.strip() for x in multi_imports]
        imports = set(single_imports + multi_imports) - {""}
        code = re.sub(r'import\s+("\w+")', '', code)
        code = re.sub(r'import\s+\((.*?)\)', '', code, flags=re.DOTALL)
        # add all packages and imports
        code = '\n'.join([f'package {p}' for p in packages]) + '\n\n' + '\n'.join([f'import {imp}' for imp in imports
                                                                                  ]) + '\n\n' + code  # add all packages
    return code


def remove_main(code, language):
    main_tokens = []
    if language == 'd':
        main_tokens = ['void main']
    elif language == 'csharp':
        main_tokens = ['public static void Main']

    for token in main_tokens:
        index = code.find(token)
        if index != -1:
            code = code[:index]
    return code


class MultiPLEDataset(CodingDataset):
    '''
    this class is for MultiPL-E Formate
    It assumes:
    - each dataset has only one language
    - append the raw completion to the code
    - the dataset other than python should contain ('prompt', 'tests', 'stop_tokens') as it is in MultiPL-E Format
    '''
    run_lang_map = {
        'multiple_cpp': 'cpp',
        'multiple_ts': 'typescript',
        'multiple_sh': 'bash',
        'multiple_cs': 'csharp',
        'multiple_go': 'go_test',
        'multiple_java': 'java',
        'multiple_lua': 'lua',
        'multiple_js': 'nodejs',
        'multiple_php': 'php',
        'multiple_pl': 'perl',
        'multiple_rkt': 'racket',
        'multiple_r': 'R',
        'multiple_rs': 'rust',
        'multiple_scala': 'scala',
        'multiple_swift': 'swift',
        'multiple_rb': 'ruby',
        'multiple_d': 'D_ut',
        'multiple_jl': 'julia',
    }
    extract_lang_map = {
        'multiple_cpp': 'cpp',
        'multiple_ts': 'typescript',
        'multiple_sh': 'bash',
        'multiple_cs': 'csharp',
        'multiple_go': 'go',
        'multiple_java': 'java',
        'multiple_lua': 'lua',
        'multiple_js': 'js',
        'multiple_php': 'php',
        'multiple_pl': 'perl',
        'multiple_rkt': 'racket',
        'multiple_r': 'r',
        'multiple_rs': 'rust',
        'multiple_scala': 'scala',
        'multiple_swift': 'swift',
        'multiple_rb': 'ruby',
        'multiple_d': 'd',
        'multiple_jl': 'julia',
    }

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(request, cls.get_table_name(request.dataset))
        return [cls._generate_single_prompt(r, request.dataset, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(request, cls.get_table_name(request.dataset))
        return cls._generate_single_prompt(row, request.dataset, request.config)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], dataset: str, config: TestConfig) -> Prompt:
        prompt = row['prompt']
        if config.extra.get('is_freeform'):
            language = cls.extract_lang_map.get(dataset, row.get('language', 'python'))
            language = language.split('.')[0]
            locale = config.locale if hasattr(config, 'locale') else 'en'

            if locale == 'zh':
                instruction = f'请按照docstring的要求补全如上代码，写出完整的代码，代码要用markdown语法包裹。代码不要包含`Main`函数'
            else:
                instruction = f'Please complete the above code according to the requirements in the docstring. Write the complete code and wrap it in markdown syntax. The code should not contain `Main` function.'

            prompt = f'```{language}\n{prompt}\n```\n\n{instruction}'

        return Prompt(id=row.get('task_id', row.get('name')),
                      prompt=prompt,
                      labels={
                          k: v for k, v in row.items() if k not in ['prompt']
                      })

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(request, cls.get_table_name(request.dataset))

        is_freeform = bool(request.config.extra.get('is_freeform'))

        if is_freeform:
            if request.dataset in ['multiple_ts', 'multiple_go', 
                    'multiple_pl', 'multiple_rkt', 'multiple_lua', 'multiple_jl', 'multiple_d',
                    'multiple_js', 'multiple_php', 'multiple_r', 'multiple_rb']:
                completion, full_code = cls._gen_sft_func_code(request, row)
            else:
                completion, full_code = cls._gen_sft_stop_token_code(request, row)
        else:
            completion, full_code = cls._gen_pretrain_code(request, row)

        language = cls.extract_lang_map.get(request.dataset, row.get('language', 'cpp'))
        language = language.split('.')[0]

        import_helper = IMPORT_HELPER.get(language, [])
        full_code = '\n'.join(import_helper) + '\n' + full_code

        if request.dataset in ['multiple_java']:
            full_code = full_code.replace('class Problem', 'class Main')

        full_code = postprocess_full_code(full_code, language)

        language = cls.run_lang_map.get(request.dataset, language)
        result = await run_code_in_sandbox(
            RunCodeRequest(
                code=full_code,
                language=language,
                compile_timeout=request.config.compile_timeout or 20,
                run_timeout=request.config.run_timeout or 20,
            ))
        accepted = result.status == RunStatus.Success

        return EvalResult(id=request.id,
                          accepted=accepted,
                          extracted_code=completion,
                          full_code=full_code,
                          tests=[EvalTestCase(
                              passed=accepted,
                              exec_info=result,
                          )])

    @classmethod
    def _gen_pretrain_code(cls, request: SubmitRequest, row: Dict[str, Any]) -> str:
        completion = request.completion
        completion = stop_token_trim(completion, row)
        language = cls.extract_lang_map.get(request.dataset, row.get('language'))
        language = language.split('.')[0]
        completion = stop_after_stop_token(completion, language)
        prompt = cls._generate_single_prompt(row, request.dataset, request.config).prompt
        full_code = f"{prompt}\n{completion}\n{row['tests']}"
        return completion, full_code

    @classmethod
    def _gen_sft_stop_token_code(cls, request: SubmitRequest, row: Dict[str, Any]) -> str:
        completion = request.completion
        language = cls.extract_lang_map.get(request.dataset, row.get('language'))
        language = language.split('.')[0]
        completion, _ = extract_code_from_freeform_completion(completion, language, first_block_only=True)
        completion = stop_token_trim(completion, row)
        completion = remove_main(completion, language)
        full_code = f"{completion}\n{row['tests']}"
        return completion, full_code

    @classmethod
    def _gen_sft_func_code(cls, request: SubmitRequest, row: Dict[str, Any]) -> str:
        completion = request.completion
        language = cls.extract_lang_map.get(request.dataset, row.get('language', 'python'))
        language = language.split('.')[0]
        completion = default_extract_helper(completion, language)
        completion = remove_main(completion, language)
        full_code = f"{completion}\n{row['tests']}"
        return completion, full_code
