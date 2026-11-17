"""Metadata compatibility must not enlarge tool permissions or soften rewards."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from resources_servers.spreadsheet.service import answer_metadata as metadata
from resources_servers.spreadsheet.service.errors import ToolError


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("G12：J15", [("Sheet1", "G12:J15")]),
        ("'Sheet1'!BD2:308", [("Sheet1", "BD2:BD308")]),
        (
            "'RAWDATA!'A1:P6,'OUTPUT!'A1:P6'",
            [("RAWDATA", "A1:P6"), ("OUTPUT", "A1:P6")],
        ),
        ("'Received'!'Received!A1:G16'", [("Received", "A1:G16")]),
        ("Sheet3'!A:G,'Sheet4'!A:G", [("Sheet3", "A:G"), ("Sheet4", "A:G")]),
        ("'O''Brien, Q1'!$A$2:$B$3", [("O'Brien, Q1", "A2:B3")]),
        ("'Invoice List‘!A1:G10", [("Invoice List", "A1:G10")]),
        ("\u00a0'Sheet 2'!B2:B8", [("Sheet 2", "B2:B8")]),
    ],
)
def test_metadata_forms(text, expected):
    assert metadata.parse_answer_position(text, default_sheet_name="Sheet1") == expected


def test_sheet_resolution_matches_upstream_order_without_fuzzy_guessing():
    book = SimpleNamespace(sheetnames=["Result", " Result "])
    assert metadata._resolve_sheet_name(book, " Result ") == " Result "
    assert metadata._resolve_sheet_name(book, "RESULT") == "Result"
    assert metadata._resolve_sheet_name(book, "  result  ") == "Result"
    assert metadata._resolve_sheet_name(book, "Results") is None


@pytest.mark.parametrize("address", ["A0", "XFE1", "A1048577", "A1!B2!C3", ""])
def test_bad_metadata_still_fails(address):
    with pytest.raises(ToolError):
        metadata.parse_answer_position(address, default_sheet_name="Sheet1")


def test_port_matches_frozen_upstream_parser_on_fixture_forms():
    """Execute only the original pure parser functions, with no RL imports."""
    source = Path(__file__).resolve().parents[3] / "reward/async_reward_api/eval.py"
    # The offline runtime contains the adapter only; local CI checks provenance.
    if not source.is_file():
        pytest.skip("upstream source not included in offline service bundle")
    nodes = ast.parse(source.read_text()).body
    names = {
        "_split_answer_position",
        "_normalize_sheet_name",
        "_normalize_cell_range",
        "_normalize_answer_position_text",
        "_is_supported_cell_range",
        "parse_answer_position",
        "_cell_range_boundaries",
    }
    pure = ast.Module(
        body=[n for n in nodes if isinstance(n, ast.FunctionDef) and n.name in names],
        type_ignores=[],
    )
    namespace = dict(vars(metadata))
    exec(compile(pure, str(source), "exec"), namespace)
    for text in [
        "'Calculation!'C11:I490'",
        "'Received'!'Received!A1:G16'",
        "B:B",
        "'Sheet1'!BD2:308",
        "G12：J15",
        "'Orders!A1:H788','Sigma_Data!A1:F428'",
        "'O''Brien, Q1'!A2:B3",
        "A3:B1",
    ]:
        assert metadata.parse_answer_position(
            text, default_sheet_name="Sheet1"
        ) == namespace["parse_answer_position"](text, default_sheet_name="Sheet1")


def test_streaming_answer_budget_and_negative_controls(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from resources_servers.spreadsheet.service.workbook import compare, resolve

    book = openpyxl.Workbook()
    book.active.title = "Result"
    book.active["A1"] = 7
    oracle, actual = tmp_path / "oracle.xlsx", tmp_path / "actual.xlsx"
    book.save(oracle)
    book.save(actual)
    before = oracle.read_bytes()
    with pytest.raises(ToolError, match="100000 cells"):
        resolve(book, "Result!A1:A100001")
    assert compare(oracle, actual, "result!A1:A100001")["total_cells"] == 100001
    assert compare(oracle, actual, "result!A1:A100001")["reward"] == 1
    assert compare(oracle, actual, "Result!B:A")["total_cells"] == 2
    book.active["A1"] = "definitely wrong"
    book.save(actual)
    assert compare(oracle, actual, "result!A1:A100001")["reward"] == 0
    assert compare(oracle, actual, "Result!B:A")["reward"] == 0
    assert oracle.read_bytes() == before
    book.close()
