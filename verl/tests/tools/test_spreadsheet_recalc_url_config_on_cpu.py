# Copyright 2026 Spreadsheet-RL Team
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

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

from omegaconf import OmegaConf

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RECALC_ENV = "SPREADSHEET_RL_RECALC_URL"
_RECALC_URL = "https://recalc.example.test/recalculate"


def _load_tool_modules():
    source_root = _REPO_ROOT / "verl" / "verl"
    original_modules = {
        name: module for name, module in sys.modules.items() if name == "verl" or name.startswith("verl.")
    }
    for name in original_modules:
        sys.modules.pop(name)

    package_paths = {
        "verl": source_root,
        "verl.tools": source_root / "tools",
        "verl.utils": source_root / "utils",
    }
    for name, path in package_paths.items():
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module
    trace_module = types.ModuleType("verl.utils.rollout_trace")
    trace_module.rollout_trace_op = lambda fn: fn
    sys.modules[trace_module.__name__] = trace_module

    try:
        return {
            name: importlib.import_module(name)
            for name in (
                "verl.tools.clear_range",
                "verl.tools.delete_rows_columns",
                "verl.tools.formula_fill",
                "verl.tools.recalculate",
                "verl.tools.schemas",
            )
        }
    finally:
        for name in tuple(sys.modules):
            if name == "verl" or name.startswith("verl."):
                sys.modules.pop(name)
        sys.modules.update(original_modules)


_TOOL_MODULES = _load_tool_modules()
OpenAIFunctionToolSchema = _TOOL_MODULES["verl.tools.schemas"].OpenAIFunctionToolSchema
_EXPECTED_TOOLS = {
    "verl.tools.formula_fill.FillFormulaTool": _TOOL_MODULES["verl.tools.formula_fill"].FillFormulaTool,
    "verl.tools.clear_range.ClearRangeTool": _TOOL_MODULES["verl.tools.clear_range"].ClearRangeTool,
    "verl.tools.delete_rows_columns.DeleteRowsTool": _TOOL_MODULES["verl.tools.delete_rows_columns"].DeleteRowsTool,
    "verl.tools.delete_rows_columns.DeleteColumnsTool": _TOOL_MODULES[
        "verl.tools.delete_rows_columns"
    ].DeleteColumnsTool,
    "verl.tools.recalculate.RecalculateAndReadTool": _TOOL_MODULES["verl.tools.recalculate"].RecalculateAndReadTool,
}


def _load_tool_entries(filename: str):
    config = OmegaConf.load(_REPO_ROOT / "configs" / "tool" / filename)
    return {str(entry.class_name): entry for entry in config.tools}


def _tool_schema(entry) -> OpenAIFunctionToolSchema:
    schema = OmegaConf.to_container(entry.tool_schema, resolve=True)
    return OpenAIFunctionToolSchema.model_validate(schema)


def test_spreadsheet_rl_recalc_url_reaches_configs_and_tool_constructors():
    with patch.dict(os.environ, {_RECALC_ENV: _RECALC_URL}):
        minimal_entries = _load_tool_entries("minimal_tools.yaml")
        minimal_recalc = minimal_entries["verl.tools.recalculate.RecalculateAndReadTool"]
        assert OmegaConf.to_container(minimal_recalc.config, resolve=True)["recalc_url"] == _RECALC_URL

        spreadsheet_entries = _load_tool_entries("spreadsheet_tools.yaml")
        recalc_entries = {
            class_name: spreadsheet_entries[class_name]
            for class_name in _EXPECTED_TOOLS
            if class_name in spreadsheet_entries
        }
        assert list(recalc_entries) == list(_EXPECTED_TOOLS)

        with patch("builtins.print"):
            for class_name, tool_class in _EXPECTED_TOOLS.items():
                entry = recalc_entries[class_name]
                config = OmegaConf.to_container(entry.config, resolve=True)
                assert isinstance(config, dict)
                assert config["recalc_url"] == _RECALC_URL

                configured_tool = tool_class(config=config, tool_schema=_tool_schema(entry))
                assert configured_tool.recalc_url == _RECALC_URL

                config.pop("recalc_url")
                environment_tool = tool_class(config=config, tool_schema=_tool_schema(entry))
                assert environment_tool.recalc_url == _RECALC_URL
