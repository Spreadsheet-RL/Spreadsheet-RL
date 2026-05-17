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
import os
from copy import deepcopy
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
from sandbox.utils.sandbox_client import run_code_in_sandbox

parent_dir = os.path.dirname(os.path.abspath(__file__))

language_mappings = {
    "cpp": "candidate(????)",
    "csharp": "F(????)",
    "D_ut": "candidate(????)",
    "go_test": "candidate(????)",
    "java": "f(????)",
    "julia": "candidate(????)",
    "nodejs": "candidate(????)",
    "lua": "candidate(????)",
    "php": "candidate(????)",
    "python": "candidate(????)",
    "perl": "$candidate->(????)",
    "R": "candidate(????)",
    "ruby": "candidate.call(????)",
    "racket": "(candidate ????)",
    "rust": "candidate(????)",
    "scala": "f(????)",
    "bash": "$(candidate ????)",
    "swift": "f(????)",
    "typescript": "candidate(????)"
}


def generate_input_prompt(language, example_list, code_snippet):
    examples = ""
    for item in example_list:
        examples += f"""```{language}
{item["code"].replace(item['inputs'], language_mappings[language]).strip()}
```
[ANSWER]
{item["inputs"]}
[/ANSWER]
"""

    prompt = f"""You will be given a {language} function f and a check function, where you only know the output of the test case. Find any input such that executing f on the input leads to the given output. There may be multiple answers, but you should only output one. Think step by step before arriving at an answer. Finally, surround the answer, with no additional words, with [ANSWER] and [/ANSWER] tags. Express your answer as a function call that when executed will give the output. 
    Make Sure The answer is WRAPPED in the format: [ANSWER] <your answer> [/ANSWER].

{examples.rstrip()}
```{language}
{code_snippet}
```
"""
    return prompt


def generate_output_prompt(language, example_list, code_snippet):
    examples = ""
    for item in example_list:
        examples += f"""```{language}
{item["code"].replace(item['outputs'], '????').strip()}
```
[ANSWER]
{item["outputs"]}
[/ANSWER]
"""

    prompt = f"""Based on the given code, which may contain errors, complete the "????" in assert statement with the output when executing the {language} code on the given test case. Do NOT output any extra information, even if the function is incorrect or incomplete. Do NOT output a description for the assert.
    Make Sure The answer is WRAPPED in the format: [ANSWER] <your answer> [/ANSWER].

{examples.rstrip()}
```{language}
{code_snippet}
```
"""
    return prompt


def create_phind_output_prompt(*args):
    code, test_input = args
    return f"""Based on the given Python code, which may contain errors, complete the assert statement with the output when executing the code on the given test case. Do NOT output any extra information, even if the function is incorrect or incomplete. Output "# done" after the assertion.

def f(n):
    return n
assert f(17) == 17 # done

def f(s):
    return s + "a"
assert f("x9j") == "x9ja" # done

{code}
assert f({test_input}) =="""


def create_output_prompt_with_reasoning(*args):
    code, test_input = args
    return f"""You are given a Python function and an assertion containing an input to the function. Complete the assertion with a literal (no unsimplified expressions, no function calls) containing the output when executing the provided code on the given input, even if the function is incorrect or incomplete. Do NOT output any extra information. Execute the program step by step before arriving at an answer, and provide the full assertion with the correct output in [ANSWER] and [/ANSWER] tags, following the examples.

[PYTHON]
def f(s):
    s = s + s
    return "b" + s + "a"
assert f("hi") == ??
[/PYTHON]
[THOUGHT]
Let's execute the code step by step:

1. The function f is defined, which takes a single argument s.
2. The function is called with the argument "hi", so within the function, s is initially "hi".
3. Inside the function, s is concatenated with itself, so s becomes "hihi".
4. The function then returns a new string that starts with "b", followed by the value of s (which is now "hihi"), and ends with "a".
5. The return value of the function is therefore "bhihia".
[/THOUGHT]
[ANSWER]
assert f("hi") == "bhihia"
[/ANSWER]

[PYTHON]
{code}
assert f({test_input}) == ??
[/PYTHON]
[THOUGHT]
"""


def create_direct_output_prompt(*args):
    code, test_input = args
    return f"""You are given a Python function and an assertion containing an input to the function. Complete the assertion with a literal (no unsimplified expressions, no function calls) containing the output when executing the provided code on the given input, even if the function is incorrect or incomplete. Do NOT output any extra information. Provide the full assertion with the correct output in [ANSWER] and [/ANSWER] tags, following the examples.

[PYTHON]
def f(n):
    return n
assert f(17) == ??
[/PYTHON]
[ANSWER]
assert f(17) == 17
[/ANSWER]

[PYTHON]
def f(s):
    return s + "a"
assert f("x9j") == ??
[/PYTHON]
[ANSWER]
assert f("x9j") == "x9ja"
[/ANSWER]

[PYTHON]
{code}
assert f({test_input}) == ??
[/PYTHON]
[ANSWER]
"""


def create_direct_input_prompt(*args):
    code, expected_output = args
    return f"""You will be given a function f and an output in the form f(??) == output. Find any input such that executing f on the input leads to the given output. There may be multiple answers, but you should only output one. In [ANSWER] and [/ANSWER] tags, complete the assertion with one such input that will produce the output when executing the function.

[PYTHON]
def f(my_list):
    count = 0
    for i in my_list:
        if len(i) % 2 == 0:
            count += 1
    return count
assert f(??) == 3
[/PYTHON]
[ANSWER]
assert f(["mq", "px", "zy"]) == 3
[/ANSWER]

[PYTHON]
def f(s1, s2):
    return s1 + s2
assert f(??) == "banana"
[/PYTHON]
[ANSWER]
assert f("ba", "nana") == "banana"
[/ANSWER]

[PYTHON]
{code}
assert f(??) == {expected_output}
[/PYTHON]
[ANSWER]
"""


def create_input_prompt_with_reasoning(*args):
    code, expected_output = args
    return f"""You will be given a function f and an output in the form f(??) == output. Your task is to find any input such that executing f on the input leads to the given output. There may be multiple answers, but only output one. First, think step by step. You MUST surround the answer with [ANSWER] and [/ANSWER] tags. Express your answer as a passing assertion containing the input and the given output.

[PYTHON]
def f(x):
    return x + 1
assert f(??) == 17
[/PYTHON]
[THOUGHT]
To find an input such that executing f on the input leads to the given output, we can work backwards from the given assertion. We know that f(??) == 17. 

Since the function f(x) returns x + 1, for f(??) to be equal to 17, the value of ?? should be 16. 
[/THOUGHT]
[ANSWER]
assert f(16) == 17
[/ANSWER]

[PYTHON]
{code}
assert f(??) == {expected_output}
[/PYTHON]
[THOUGHT]
"""


class CruxEvalDataset(CodingDataset):
    WRAP_PROMPT_INS = 'Please respond in English and use Markdown format.####Instruction:'
    WRAP_PROMPT_RES = 'Response:'

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'input', 'output', 'code'],
        )
        if request.dataset == 'cruxeval_x':
            with open(os.path.join(parent_dir, '../../assets/cruxeval_x_utils', 'examples.json'), 'r') as f:
                examples = json.load(f)
            if mode := request.config.extra.get('mode'):
                return [cls._generate_single_prompt_x(r, examples, request.config, mode) for r in rows]
            return [cls._generate_single_prompt_x(r, examples, request.config) for r in rows
                   ] + [cls._generate_single_prompt_x(r, examples, request.config, 'output') for r in rows]
        else:
            if mode := request.config.extra.get('mode'):
                return [cls._generate_single_prompt(r, request.config, mode) for r in rows]
            return [cls._generate_single_prompt(r, request.config) for r in rows
                   ] + [cls._generate_single_prompt(r, request.config, 'output') for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['id', 'input', 'output', 'code'])
        mode = request.config.extra.get('mode', 'output')
        return cls._generate_single_prompt(row, request.config, mode)

    @classmethod
    def _generate_single_prompt_x(cls,
                                  row: Dict[str, Any],
                                  examples: dict,
                                  config: TestConfig,
                                  mode: str = 'input') -> Prompt:
        Code = row['code']

        autoeval_wrap_prompt = config.extra.get('coding_wrap_prompt') == True
        lang = '_'.join(row['id'].split('_')[:-1])

        if mode == 'input':
            prompt = generate_input_prompt(lang, examples[lang], Code + row['input'])
        else:
            prompt = generate_output_prompt(lang, examples[lang], Code + row['output'])

        if autoeval_wrap_prompt:
            prompt = f'{cls.WRAP_PROMPT_INS}\n{prompt}\n{cls.WRAP_PROMPT_RES}\n'

        return Prompt(id=row['id'], prompt=prompt, labels={'mode': mode})

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig, mode: str = 'input') -> Prompt:
        Code = row['code']

        autoeval_wrap_prompt = config.extra.get('coding_wrap_prompt') == True

        if config.extra.get('use_cot'):
            if mode == 'input':
                prompt = create_input_prompt_with_reasoning(Code, row['output'])
            else:
                prompt = create_output_prompt_with_reasoning(Code, row['input'])
        else:
            if mode == 'input':
                prompt = create_direct_input_prompt(Code, row['output'])
            else:
                if config.extra.get('phind_output'):
                    prompt = create_phind_output_prompt(Code, row['input'])
                else:
                    prompt = create_direct_output_prompt(Code, row['input'])

        if autoeval_wrap_prompt:
            prompt = f'{cls.WRAP_PROMPT_INS}\n{prompt}\n{cls.WRAP_PROMPT_RES}\n'

        cfg = deepcopy(config.extra)
        cfg['mode'] = mode

        return Prompt(id=row['id'], prompt=prompt, labels={'mode': mode})

    @classmethod
    async def get_test_info_by_id(cls, id: int, table_name='cruxeval') -> str:
        """ This function only used for unittest case.
        """
        if table_name == 'cruxeval_x':
            row = await get_row_by_id_in_table(GetPromptByIdRequest(dataset=table_name, config=TestConfig(), id=id),
                                               cls.get_table_name(table_name),
                                               columns=['input', 'output', 'gt', 'id'])
            return row['input'], row['output'], row['gt'], row['id']
        row = await get_row_by_id_in_table(GetPromptByIdRequest(dataset=table_name, config=TestConfig(), id=id),
                                           cls.get_table_name(table_name),
                                           columns=['input', 'output'])
        return row['input'], row['output']

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        stop_words = ["# done", "[/ANSWER]"]
        row = await get_row_by_id_in_table(request,
                                           cls.get_table_name(request.dataset),
                                           columns=['input', 'output', 'code', 'id'])

        mode = request.config.extra['mode']

        completion = request.completion
        for st in stop_words:
            completion = completion.split(st)[0]

        if '[ANSWER]' in completion:
            completion = completion.split('[ANSWER]')[1].strip()

        assert mode in ['input', 'output'], f'Invalid code_mode: {mode}'
        code = row['code']

        if mode == 'input':
            if request.dataset == 'cruxeval_x':
                lang = '_'.join(row['id'].split('_')[:-1])
                if language_mappings[lang][:language_mappings[lang].index('????')] not in completion:
                    completion = language_mappings[lang].replace('????', completion)
                completion = row['input'].replace(language_mappings[lang], completion)
                full_code = code + completion
            else:
                lang = 'python'
                if "assert f" in completion:
                    completion = "f" + completion.split("assert f")[1].strip()
                if "==" in completion:
                    completion = completion.split("==")[0].strip()
                output = row['output']
                full_code = f'{code}\nassert {completion} == {output}'
        else:
            if request.dataset == 'cruxeval_x':
                lang = '_'.join(row['id'].split('_')[:-1])
                completion = row['output'].replace('????', completion)
                full_code = code + completion
            else:
                lang = 'python'
                if "assert f" in completion:
                    completion = "f" + completion.split("assert f")[1].strip()
                if "==" in completion:
                    completion = completion.split("==")[1].strip()
                input = row['input']
                full_code = f'{code}\nassert {completion} == f({input})'
        result = await run_code_in_sandbox(
            RunCodeRequest(code=full_code, language=lang, compile_timeout=30, run_timeout=30))
        accepted = result.status == RunStatus.Success

        return EvalResult(id=request.id,
                          accepted=accepted,
                          extracted_code=completion,
                          full_code=full_code,
                          tests=[EvalTestCase(
                              passed=accepted,
                              exec_info=result,
                          )])
