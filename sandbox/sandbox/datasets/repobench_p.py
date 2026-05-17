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

import hashlib
from typing import Any, Dict, List

from transformers import AutoTokenizer

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


def comment(code: str, language: str):
    if language == "python":
        return "\n".join([f"# {line}" for line in code.split("\n")])
    elif language == "java":
        return "\n".join([f"// {line}" for line in code.split("\n")])
    else:
        raise ValueError("language must be one of [python, java]")


class RepobenchPDataset(CodingDataset):

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'file_path', 'context', 'import_statement', "code", "gold_snippet_index", "next_line"],
        )
        for r in rows:
            r['lang'] = request.dataset.split("_")[-1]
            ensure_json(r, 'context')
        return [cls._generate_single_prompt(r, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'file_path', 'context', 'import_statement', "code", "gold_snippet_index", "next_line"])

        row['lang'] = request.dataset.split("_")[-1]
        ensure_json(row, 'context')
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        mode = config.extra.get('retrieval_mode', "oracle-filled")
        tokenizer = AutoTokenizer.from_pretrained("assets/tokenizer/gpt2")
        max_prompt_length = config.extra.get('max_prompt_length', 8000)
        token_size = 0
        prompt_list = []

        code = "\n".join(row['code'].split("\n")[-60:])  # 保存最多60行
        code_path = row['file_path']
        import_statement = row['import_statement']
        if row['lang'] == "python":
            code = f"# Path: {code_path}\n{import_statement}\n{code}"
        elif row['lang'] == "java":
            if code.split("\n")[0].startswith("package"):
                code = "\n".join(code.split("\n")[:1] + [import_statement] + code.split("\n")[1:])
                code = f"// Path: {code_path}\n{code}\n"
            else:
                code = f"// Path: {code_path}\n{import_statement}\n{code}\n"

        code_tokens = tokenizer.encode(code)
        if len(code_tokens) > 1600:  #本文件 token控制在1600
            code_tokens = code_tokens[-1600:]
            code = tokenizer.decode(code_tokens)
        token_size += len(tokenizer.encode(code))
        prompt_list.append(code)

        if mode == "oracle-filled":
            idx2context_pair = {}
            for i in range(len(row['context'])):
                snippet_path = row['context'][i]['path']
                snippet_context = row['context'][i]['snippet']
                # comment the random snippet context
                snippet_context = comment(snippet_context, row['lang'])

                # concat path and context
                if row['lang'] == "python":
                    snippet_context = f"# Path: {snippet_path}\n{snippet_context}\n"
                elif row['lang'] == "java":
                    snippet_context = f"// Path: {snippet_path}\n{snippet_context}\n"

                snippet_context_tokens = len(tokenizer.encode(snippet_context))
                idx2context_pair[i] = snippet_context, snippet_context_tokens
            # 选中gold snippet
            gold_snippet_idx = row['gold_snippet_index']
            if row['gold_snippet_index'] > 0:
                token_size += idx2context_pair[gold_snippet_idx][1]
                prompt_list.append(idx2context_pair[gold_snippet_idx][0])
            for idx, cur_context, cur_token_size in [
                (k, idx2context_pair[k][0], idx2context_pair[k][1]) for k in idx2context_pair
            ]:
                if idx == gold_snippet_idx:
                    continue
                if cur_token_size + token_size > max_prompt_length:
                    break
                token_size += cur_token_size
                prompt_list.append(cur_context)

        code = prompt_list[0]
        contexts = prompt_list[1:]
        # context: 按顺序取到满足max_prompt_length长度的context，context之间通过md5排序
        contexts.sort(key=lambda x: hashlib.md5(x.encode("utf8")).hexdigest())
        prompt = "".join(contexts) + code

        return Prompt(id=row['id'], prompt=prompt, labels={'next_line': row['next_line']})

    @classmethod
    async def get_test_info_by_id(cls, table_name, id: int) -> str:
        """ This function only used for unittest case.
        """
        row = await get_row_by_id_in_table(
            GetPromptByIdRequest(dataset=table_name, config=TestConfig(), id=id),
            cls.get_table_name(table_name),
            columns=['id', 'file_path', 'context', 'import_statement', "code", "gold_snippet_index", "next_line"])
        return None, row['next_line']

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'file_path', 'context', 'import_statement', "code", "gold_snippet_index", "next_line"])
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
