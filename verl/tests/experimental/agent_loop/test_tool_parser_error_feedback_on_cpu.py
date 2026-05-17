# Copyright 2025 Bytedance Ltd. and/or its affiliates
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
from pathlib import Path

import pytest
from omegaconf import OmegaConf

from verl.experimental.agent_loop.tool_parser import (
    TOOL_PARSER_ERROR_FUNCTION_NAME,
    HermesToolParser,
    Qwen3XMLToolParser,
)
from verl.tools.schemas import OpenAIFunctionToolSchema


class DummyTokenizer:
    pad_token = "<pad>"

    def __init__(self, text: str):
        self.text = text

    def decode(self, responses_ids, *args, **kwargs):
        return self.text


def _tool_schema(name: str = "code_interpreter") -> OpenAIFunctionToolSchema:
    return OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Execute code.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python code."},
                    },
                    "required": ["code"],
                },
            },
        }
    )


def _spreadsheet_tool_schemas() -> list[OpenAIFunctionToolSchema]:
    config_path = Path(__file__).parents[3] / "configs/tool/spreadsheet_tools.yaml"
    config = OmegaConf.load(config_path)
    return [
        OpenAIFunctionToolSchema.model_validate(OmegaConf.to_container(tool.tool_schema, resolve=True))
        for tool in config.tools
    ]


def _assert_parser_error(tool_calls, expected_text: str):
    assert len(tool_calls) == 1
    assert tool_calls[0].name == TOOL_PARSER_ERROR_FUNCTION_NAME
    assert tool_calls[0].internal is True
    payload = json.loads(tool_calls[0].arguments)
    assert expected_text in payload["message"]


@pytest.mark.asyncio
async def test_hermes_invalid_json_returns_feedback_call():
    text = '</think><tool_call>{"name": "code_interpreter", "arguments": {bad}}</tool_call>'
    parser = HermesToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([])

    _assert_parser_error(tool_calls, "invalid JSON")


@pytest.mark.asyncio
async def test_hermes_tool_call_inside_think_returns_feedback_call():
    text = '<think><tool_call>{"name": "code_interpreter", "arguments": {}}</tool_call></think>done'
    parser = HermesToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([])

    _assert_parser_error(tool_calls, "inside reasoning content")


@pytest.mark.asyncio
async def test_hermes_mixed_valid_and_invalid_json_fails_closed():
    text = """
</think>
<tool_call>{"name":"code_interpreter","arguments":{"code":"print(1)"}}</tool_call>
<tool_call>{bad}</tool_call>
"""
    parser = HermesToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([])

    _assert_parser_error(tool_calls, "invalid JSON")


@pytest.mark.asyncio
async def test_hermes_reasoning_tool_call_plus_valid_later_fails_closed():
    text = """
<think><tool_call>{"name":"code_interpreter","arguments":{"code":"bad"}}</tool_call></think>
<tool_call>{"name":"code_interpreter","arguments":{"code":"print(1)"}}</tool_call>
"""
    parser = HermesToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([])

    _assert_parser_error(tool_calls, "inside reasoning content")


@pytest.mark.asyncio
async def test_qwen3_coder_array_json_literals_are_safe():
    parser = Qwen3XMLToolParser(
        DummyTokenizer(
            """
<tool_call>
<function=example>
<parameter=items>[true, false, null]</parameter>
</function>
</tool_call>
"""
        )
    )
    schema = OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": "example",
                "description": "Example.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "description": "Items."},
                    },
                    "required": ["items"],
                },
            },
        }
    )

    _, tool_calls = await parser.extract_tool_calls([], [schema])

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "example"
    assert json.loads(tool_calls[0].arguments) == {"items": [True, False, None]}


@pytest.mark.asyncio
async def test_qwen3_coder_array_parser_does_not_eval(tmp_path):
    target = tmp_path / "should_not_exist"
    parser = Qwen3XMLToolParser(
        DummyTokenizer(
            f"""
<tool_call>
<function=example>
<parameter=items>__import__("pathlib").Path("{target}").touch()</parameter>
</function>
</tool_call>
"""
        )
    )
    schema = OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": "example",
                "description": "Example.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "description": "Items."},
                    },
                    "required": ["items"],
                },
            },
        }
    )

    _, tool_calls = await parser.extract_tool_calls([], [schema])

    assert len(tool_calls) == 1
    assert json.loads(tool_calls[0].arguments) == {
        "items": f'__import__("pathlib").Path("{target}").touch()'
    }
    assert not target.exists()


@pytest.mark.asyncio
async def test_qwen3_coder_valid_xml_tool_call():
    text = """
<tool_call>
<function=code_interpreter>
<parameter=code>
print("hello")
</parameter>
</function>
</tool_call>
"""
    parser = Qwen3XMLToolParser(DummyTokenizer(text))

    content, tool_calls = await parser.extract_tool_calls([], [_tool_schema()])

    assert content.strip() == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "code_interpreter"
    assert json.loads(tool_calls[0].arguments) == {"code": 'print("hello")'}


@pytest.mark.asyncio
async def test_hermes_spreadsheet_json_arguments_keep_types():
    text = """
</think><tool_call>{"name":"delete_rows","arguments":{"sheet_name":"Sheet1","rows":["2:3","7:9"]}}</tool_call>
"""
    parser = HermesToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([], _spreadsheet_tool_schemas())

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "delete_rows"
    assert json.loads(tool_calls[0].arguments) == {"sheet_name": "Sheet1", "rows": ["2:3", "7:9"]}


@pytest.mark.asyncio
async def test_qwen3_coder_spreadsheet_xml_arguments_keep_types():
    parser = Qwen3XMLToolParser(
        DummyTokenizer(
            """
<tool_call>
<function=find_cells>
<parameter=sheet_name>Sheet1</parameter>
<parameter=query>Revenue</parameter>
<parameter=case_sensitive>false</parameter>
<parameter=max_results>10</parameter>
<parameter=return>all</parameter>
</function>
</tool_call>
"""
        )
    )

    _, tool_calls = await parser.extract_tool_calls([], _spreadsheet_tool_schemas())

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "find_cells"
    assert json.loads(tool_calls[0].arguments) == {
        "sheet_name": "Sheet1",
        "query": "Revenue",
        "case_sensitive": False,
        "max_results": 10,
        "return": "all",
    }


@pytest.mark.asyncio
async def test_qwen3_coder_spreadsheet_xml_array_arguments_keep_list_type():
    text = """
<tool_call>
<function=recalculate_and_read>
<parameter=cell_ranges>["A1", "Sheet1!B2:C3"]</parameter>
</function>
</tool_call>
"""
    parser = Qwen3XMLToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([], _spreadsheet_tool_schemas())

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "recalculate_and_read"
    assert json.loads(tool_calls[0].arguments) == {"cell_ranges": ["A1", "Sheet1!B2:C3"]}


@pytest.mark.asyncio
async def test_qwen3_coder_missing_tool_call_end_returns_feedback_call():
    text = """
<tool_call>
<function=code_interpreter>
<parameter=code>
print("hello")
</parameter>
</function>
"""
    parser = Qwen3XMLToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([], [_tool_schema()])

    _assert_parser_error(tool_calls, "unbalanced XML tool call tags")


@pytest.mark.asyncio
async def test_qwen3_coder_missing_function_returns_feedback_call():
    text = """
<tool_call>
<parameter=code>
print("hello")
</parameter>
</tool_call>
"""
    parser = Qwen3XMLToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([], [_tool_schema()])

    _assert_parser_error(tool_calls, "invalid XML tool call content")


@pytest.mark.asyncio
async def test_qwen3_coder_malformed_function_returns_feedback_call():
    text = """
<tool_call>
<function=code_interpreter
<parameter=code>
print("hello")
</parameter>
</function>
</tool_call>
"""
    parser = Qwen3XMLToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([], [_tool_schema()])

    _assert_parser_error(tool_calls, "could not parse")


@pytest.mark.asyncio
async def test_qwen3_coder_tag_like_text_inside_parameter_is_preserved():
    text = """
<tool_call>
<function=code_interpreter>
<parameter=code>print("<parameter=x>")
print("</function>")</parameter>
</function>
</tool_call>
"""
    parser = Qwen3XMLToolParser(DummyTokenizer(text))

    content, tool_calls = await parser.extract_tool_calls([], [_tool_schema()])

    assert content.strip() == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "code_interpreter"
    assert json.loads(tool_calls[0].arguments) == {"code": 'print("<parameter=x>")\nprint("</function>")'}


@pytest.mark.asyncio
async def test_qwen3_coder_mixed_valid_and_malformed_blocks_fail_closed():
    text = """
<tool_call>
<function=code_interpreter>
<parameter=code>print("ok")</parameter>
</function>
</tool_call>
<tool_call>
<function=code_interpreter>
<parameter=code>print("bad")</parameter>
</tool_call>
"""
    parser = Qwen3XMLToolParser(DummyTokenizer(text))

    _, tool_calls = await parser.extract_tool_calls([], [_tool_schema()])

    _assert_parser_error(tool_calls, "unbalanced XML function tags")
