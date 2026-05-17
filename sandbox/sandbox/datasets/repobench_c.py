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

from typing import List

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


def get_first_line_not_comment(code: str, language: str = "python"):
    """
    This function gets the first line of code that is not a comment.

    Args:
    code: Str, the code

    Returns:
    Str, the first line of code that is not a comment or the first line of code if there is no line that is not a comment
    """

    # check if the language is valid
    assert language in ["python", "java"], "language must be one of [python, java]"

    # first remove the \n at the beginning of the code
    code = code.lstrip('\n')

    lines = code.split('\n')
    in_multiline_comment = False

    if language == "python":
        for line in lines:
            # if the line is empty, then skip
            if not line.strip():
                continue
            # if the line is a start of a multiline comment, then set the in_multiline_comment to True and skip
            if not in_multiline_comment and (line.strip().startswith('"""') or line.strip().startswith("'''")):
                in_multiline_comment = True
                continue
            # if the line is the end of a multiline comment, then set the in_multiline_comment to False and skip
            if in_multiline_comment and (line.strip().endswith('"""') or line.strip().endswith("'''")):
                in_multiline_comment = False
                continue
            # if the line is in a multiline comment, then skip
            if in_multiline_comment:
                continue
            # if the line is a single line comment, then skip
            if line.strip().startswith('#'):
                continue
            # if the line is not a comment, then return the line
            return line

    elif language == "java":
        for line in lines:
            # if the line is empty, then skip
            if not line.strip():
                continue
            # if the line is a start of a multiline comment, then set the in_multiline_comment to True and skip
            if not in_multiline_comment and line.strip().startswith('/*'):
                in_multiline_comment = True
                continue
            # if the line is the end of a multiline comment, then set the in_multiline_comment to False and skip
            if in_multiline_comment and line.strip().endswith('*/'):
                in_multiline_comment = False
                continue
            # if the line is in a multiline comment, then skip
            if in_multiline_comment:
                continue
            # if the line is a single line comment, then skip
            if line.strip().startswith('//'):
                continue
            # if the line is not a comment, then return the line
            return line
    # if we cannot find a line that is not a comment, then return the first line
    return lines[0]


class RepobenchCDataset(CodingDataset):

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'file_path', 'context', 'import_statement', "code", "prompt",
                     "next_line"],  #prompt= context + file_path+import_statement+code
        )
        return [Prompt(id=r['id'], prompt=r['prompt'], labels={'next_line': r['next_line']}) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'file_path', 'context', 'import_statement', "code", "prompt", "next_line"])

        return Prompt(id=row['id'], prompt=row['prompt'], labels={'next_line': row['next_line']})

    @classmethod
    async def get_test_info_by_id(cls, table_name, id: int) -> str:
        """ This function only used for unittest case.
        """
        row = await get_row_by_id_in_table(GetPromptByIdRequest(dataset=table_name, config=TestConfig(), id=id),
                                           cls.get_table_name(table_name),
                                           columns=['id', 'prompt', 'next_line'])
        return row['prompt'], row['next_line']

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['file_path', 'context', 'import_statement', "code", "prompt", "next_line"])
        completion = request.completion

        lang = request.dataset.split("_")[-1]
        completion = get_first_line_not_comment(completion, lang)

        next_line = row['next_line']
        if lang == "python":
            full_code = f'''
def check():
    assert {completion.split()} == {next_line.split()}
check()    
'''
        elif lang == "java":
            if completion.split() == next_line.split():
                full_code = '''
public class Main {
    public static void main(String[] args) {
        String completion = "some value"; 
        String nextLine = "some value";
        assert completion.equals(nextLine);
    }
}'''
            else:
                full_code = '''
public class Main {
    public static void main(String[] args) {
        String completion = "some value"; 
        String nextLine = "some other value";
        assert completion.equals(nextLine);
    }
}'''

        result = await run_code_in_sandbox(RunCodeRequest(code=full_code, language=lang, run_timeout=30))
        accepted = result.status == RunStatus.Success

        return EvalResult(id=request.id,
                          accepted=accepted,
                          extracted_code=completion,
                          full_code=full_code,
                          tests=[EvalTestCase(
                              passed=accepted,
                              exec_info=result,
                          )])
