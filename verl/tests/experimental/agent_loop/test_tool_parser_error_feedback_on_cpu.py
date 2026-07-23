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
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from omegaconf import OmegaConf


def _load_parser_modules():
    sentinel = object()
    module_names = (
        "verl",
        "verl.experimental",
        "verl.experimental.agent_loop",
        "verl.experimental.agent_loop.tool_parser",
        "verl.tools",
        "verl.tools.schemas",
        "verl.utils",
        "verl.utils.ray_utils",
        "verl.utils.rollout_trace",
    )
    original_modules = {name: sys.modules.get(name, sentinel) for name in module_names}
    module_defs = {
        "verl": types.ModuleType("verl"),
        "verl.experimental": types.ModuleType("verl.experimental"),
        "verl.experimental.agent_loop": types.ModuleType("verl.experimental.agent_loop"),
        "verl.tools": types.ModuleType("verl.tools"),
        "verl.utils": types.ModuleType("verl.utils"),
        "verl.utils.ray_utils": types.ModuleType("verl.utils.ray_utils"),
        "verl.utils.rollout_trace": types.ModuleType("verl.utils.rollout_trace"),
    }
    module_defs["verl"].__path__ = []
    module_defs["verl.experimental"].__path__ = []
    module_defs["verl.experimental.agent_loop"].__path__ = []
    module_defs["verl.tools"].__path__ = []
    module_defs["verl.utils"].__path__ = []
    module_defs["verl.utils.ray_utils"].get_event_loop = asyncio.get_event_loop
    module_defs["verl.utils.rollout_trace"].rollout_trace_op = lambda fn: fn
    for name, module in module_defs.items():
        sys.modules[name] = module

    try:
        repo_root = Path(__file__).parents[3]
        loaded = {}
        for name, path in (
            ("verl.tools.schemas", repo_root / "verl" / "tools" / "schemas.py"),
            (
                "verl.experimental.agent_loop.tool_parser",
                repo_root / "verl" / "experimental" / "agent_loop" / "tool_parser.py",
            ),
        ):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            sys.modules[name] = module
            spec.loader.exec_module(module)
            loaded[name] = module

        schemas = loaded["verl.tools.schemas"]
        parser = loaded["verl.experimental.agent_loop.tool_parser"]
        return (
            parser.TOOL_PARSER_ERROR_FUNCTION_NAME,
            parser.HermesToolParser,
            parser.Qwen3XMLToolParser,
            schemas.OpenAIFunctionToolSchema,
        )
    finally:
        for name, original in original_modules.items():
            if original is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


(
    TOOL_PARSER_ERROR_FUNCTION_NAME,
    HermesToolParser,
    Qwen3XMLToolParser,
    OpenAIFunctionToolSchema,
) = _load_parser_modules()


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
    config_path = next(
        path
        for path in (
            Path(__file__).parents[3] / "configs/tool/spreadsheet_tools.yaml",
            Path(__file__).parents[4] / "configs/tool/spreadsheet_tools.yaml",
        )
        if path.is_file()
    )
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


def test_spreadsheet_tool_schema_preserves_items_unions_and_enums():
    schemas = {schema.function.name: schema for schema in _spreadsheet_tool_schemas()}
    serialized = {name: schema.model_dump(exclude_unset=True, exclude_none=True) for name, schema in schemas.items()}

    inspect_parameters = schemas["inspect_range"].function.parameters
    delete_rows = schemas["delete_rows"].function.parameters.properties["rows"]
    recalc_ranges = schemas["recalculate_and_read"].function.parameters.properties["cell_ranges"]
    write_data = schemas["write_range"].function.parameters.properties["data"]

    assert inspect_parameters.required == ["range"]
    assert "ranges" not in inspect_parameters.properties
    assert delete_rows.items is not None and delete_rows.items.type == "string"
    assert recalc_ranges.items is not None and recalc_ranges.items.type == "string"
    assert write_data.type == ["array", "string", "number", "boolean"]
    format_properties = schemas["format_range"].function.parameters.properties
    assert "shrink_to_fit" in format_properties
    assert "alignment" not in format_properties
    assert "background_color" not in format_properties
    assert format_properties["horizontal_alignment"].enum == [
        "general",
        "left",
        "center",
        "right",
        "fill",
        "justify",
        "centerContinuous",
        "distributed",
    ]
    workbook_schemas = {name: schema for name, schema in schemas.items() if name != "code_interpreter"}
    assert len(workbook_schemas) == 11
    assert all(schema.function.parameters.properties["path"].type == "string" for schema in workbook_schemas.values())
    assert "path" not in schemas["code_interpreter"].function.parameters.properties
    for name in (
        "inspect_range",
        "find_cells",
        "write_range",
        "format_range",
        "fill_formula",
        "clear_range",
        "delete_rows",
        "delete_columns",
    ):
        assert "omit when" in schemas[name].function.parameters.properties["sheet_name"].description
    assert "cannot locate blank cells" in schemas["find_cells"].function.parameters.properties["query"].description
    assert "explicit rectangle's shape" in write_data.description
    assert schemas["manage_sheet"].function.parameters.properties["action"].enum == [
        "create",
        "rename",
        "delete",
        "copy",
        "move",
        "hide",
        "unhide",
    ]
    assert schemas["find_cells"].function.parameters.properties["match"].enum == [
        "contains",
        "equals",
        "prefix",
        "regex",
    ]
    assert schemas["find_cells"].function.parameters.properties["search_in"].enum == [
        "values",
        "formulas",
        "both",
    ]
    assert schemas["find_cells"].function.parameters.properties["return"].enum == ["first", "all"]
    assert serialized["write_range"]["function"]["parameters"]["properties"]["data"]["type"] == [
        "array",
        "string",
        "number",
        "boolean",
    ]


def test_spreadsheet_rl_rollout_response_limit_covers_inspect_range_budget():
    repo_root = Path(__file__).parents[4]
    rollout = OmegaConf.load(repo_root / "configs" / "spreadsheet_rl_multiturn_grpo.yaml")
    tools = OmegaConf.load(repo_root / "configs" / "tool" / "spreadsheet_tools.yaml")
    inspect = next(tool for tool in tools.tools if tool.class_name == "verl.tools.inspect_range.InspectRangeTool")

    assert rollout.actor_rollout_ref.rollout.multi_turn.max_tool_response_length >= inspect.config.max_response_chars


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_data", "expected"),
    [
        ("[[1, 2]]", [[1, 2]]),
        ("done", "done"),
        ("42", 42),
        ("true", True),
    ],
)
async def test_qwen3_coder_union_parameter_accepts_scalar_or_array(raw_data, expected):
    schemas = _spreadsheet_tool_schemas()
    schema = next(schema for schema in schemas if schema.function.name == "write_range")
    parser = Qwen3XMLToolParser(
        DummyTokenizer(
            """
<tool_call>
<function=write_range>
<parameter=range>A1:B1</parameter>
<parameter=data>{raw_data}</parameter>
</function>
</tool_call>
""".format(raw_data=raw_data)
        )
    )

    _, tool_calls = await parser.extract_tool_calls([], [schema])

    assert json.loads(tool_calls[0].arguments) == {"range": "A1:B1", "data": expected}


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
    assert json.loads(tool_calls[0].arguments) == {"items": f'__import__("pathlib").Path("{target}").touch()'}
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
async def test_qwen3_coder_spreadsheet_xml_inspect_range_accepts_sheet_name():
    parser = Qwen3XMLToolParser(
        DummyTokenizer(
            """
<tool_call>
<function=inspect_range>
<parameter=range>A1:B2</parameter>
<parameter=sheet_name>Sheet1</parameter>
<parameter=include_details>true</parameter>
</function>
</tool_call>
"""
        )
    )

    _, tool_calls = await parser.extract_tool_calls([], _spreadsheet_tool_schemas())

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "inspect_range"
    assert json.loads(tool_calls[0].arguments) == {
        "range": "A1:B2",
        "sheet_name": "Sheet1",
        "include_details": True,
    }


@pytest.mark.asyncio
async def test_qwen3_coder_spreadsheet_xml_inspect_range_accepts_comma_separated_ranges():
    parser = Qwen3XMLToolParser(
        DummyTokenizer(
            """
<tool_call>
<function=inspect_range>
<parameter=range>A1:B2,J1:J2</parameter>
<parameter=sheet_name>Sheet1</parameter>
</function>
</tool_call>
"""
        )
    )

    _, tool_calls = await parser.extract_tool_calls([], _spreadsheet_tool_schemas())

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "inspect_range"
    assert json.loads(tool_calls[0].arguments) == {
        "range": "A1:B2,J1:J2",
        "sheet_name": "Sheet1",
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
