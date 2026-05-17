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

from fastapi import HTTPException

from sandbox.database import get_row_by_id_in_table, get_rows_in_table
from sandbox.datasets.types import (
    CodingDataset,
    EvalResult,
    GeneralStdioTest,
    GetPromptByIdRequest,
    GetPromptsRequest,
    Prompt,
    SubmitRequest,
    TestConfig,
)
from sandbox.utils.common import ensure_json
from sandbox.utils.extraction import default_extract_helper
from sandbox.utils.testing import check_stdio_test_cases_parallel


class CommonOJDataset(CodingDataset):

    language_prompts = {
        'en': {
            'default':
                "Please implement complete executable code with an entry point in {language}. Your program should read input from standard input and write output to standard output.",
            'cpp':
                "Please implement complete executable code including a main function in C++. Your program should read input from standard input and write output to standard output.",
            'python':
                "Please implement a complete Python script. Your program should read input from standard input and write output to standard output.",
            'java':
                "Please implement a complete Java program with a public class named Main. Your class should include a public static void main(String[] args) method. Your program should read input from System.in and write output to System.out."
        },
        'zh': {
            'default':
                "请使用 {language} 实现一个完整的可执行程序，包含程序入口。你的程序应该从标准输入读取数据，并将结果输出到标准输出。",
            'cpp':
                "请使用 C++ 实现一个完整的可执行程序，包含 main 函数。你的程序应该从标准输入读取数据，并将结果输出到标准输出。",
            'python':
                "请编写一个完整的 Python 脚本。你的程序应该从标准输入读取数据，并将结果输出到标准输出。",
            'java':
                "请实现一个完整的 Java 程序，包含一个名为 Main 的公共类。你的类中应包含 public static void main(String[] args) 方法。你的程序应该从 System.in 读取输入，并将结果输出到 System.out。"
        }
    }

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
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'labels', 'content'])
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        prompt = row['content']
        locale = getattr(config, 'locale', 'en')
        language = config.language

        language_prompt = cls.language_prompts.get(locale, cls.language_prompts['en'])
        specific_prompt = language_prompt.get(language, language_prompt['default'])

        prompt += f"\n\n{specific_prompt.format(language=language)}"

        return Prompt(id=row['id'], prompt=prompt, labels=ensure_json(row, 'labels'))

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        if not request.config.language:
            raise HTTPException(status_code=400,
                                detail=f'config.language field must exist for CommonOJDataset, got None')
        row = await get_row_by_id_in_table(request, cls.get_table_name(request.dataset), columns=['test'])
        cases = [GeneralStdioTest(**case) for case in ensure_json(row, 'test')]
        code = default_extract_helper(request.completion, request.config.language, request.config.custom_extract_logic)
        outcomes = await check_stdio_test_cases_parallel(code, cases, request.config)
        return EvalResult(id=request.id,
                          accepted=all([o.passed for o in outcomes]),
                          extracted_code=code,
                          tests=outcomes)
