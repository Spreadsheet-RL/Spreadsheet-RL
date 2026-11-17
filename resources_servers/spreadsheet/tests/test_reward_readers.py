"""Reward reader regressions; run XLSX dependencies on the existing Modal runtime."""

import io
from zipfile import ZipFile

import pytest


openpyxl = pytest.importorskip("openpyxl")

from resources_servers.spreadsheet.service.errors import ToolError  # noqa: E402
from resources_servers.spreadsheet.service.workbook import (  # noqa: E402
    cell_equal,
    compare,
    load,
    split_ranges,
)


def workbook(path, value=7):
    wb = openpyxl.Workbook()
    wb.active.title = "Result"
    wb.active["A1"] = value
    wb.save(path)
    wb.close()
    return path


def test_reward_accepts_24_metadata_ranges_without_changing_tool_limit(tmp_path):
    oracle = workbook(tmp_path / "oracle.xlsx")
    actual = workbook(tmp_path / "actual.xlsx")
    address = ",".join(f"Result!A{row}" for row in range(1, 25))
    with pytest.raises(ToolError, match="1–20"):
        split_ranges(address)
    assert compare(oracle, actual, address) == {
        "reward": 1.0,
        "matched_cells": 24,
        "total_cells": 24,
        "cell_accuracy": 1.0,
    }


def test_reward_ignores_invalid_conditional_formatting_but_reads_cell_values(tmp_path):
    oracle = workbook(tmp_path / "oracle.xlsx")
    actual = tmp_path / "actual.xlsx"
    with (
        ZipFile(io.BytesIO(oracle.read_bytes())) as source,
        ZipFile(actual, "w") as target,
    ):
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = content.replace(
                    b"</worksheet>",
                    b'<conditionalFormatting sqref="A1"><cfRule type="cellIs" '
                    b'operator="invalidOperator" priority="1"><formula>0</formula>'
                    b"</cfRule></conditionalFormatting></worksheet>",
                )
            target.writestr(name, content)
    before = actual.read_bytes()
    with pytest.raises(ToolError):
        load(actual)
    assert compare(oracle, actual, "Result!A1")["reward"] == 1
    assert actual.read_bytes() == before
    assert (
        compare(workbook(tmp_path / "wrong.xlsx", 8), actual, "Result!A1")["reward"]
        == 0
    )


def test_strict_reward_keeps_original_rounding_boundary_and_type_rules():
    assert not cell_equal(13802196.075000001, 13802196.075)
    assert cell_equal(1.231, 1.232)
    assert cell_equal(None, "")
    assert not cell_equal(True, 1)
    assert not cell_equal("4 days, 18:50 hours", "4 days, 18:49 hours")


def test_streaming_reader_scores_trailing_empty_cells_and_missing_sheets(tmp_path):
    oracle = workbook(tmp_path / "oracle.xlsx")
    actual = workbook(tmp_path / "actual.xlsx")
    assert compare(oracle, actual, "Result!A1:C4")["matched_cells"] == 12
    wb = openpyxl.load_workbook(actual)
    wb.active.title = "Other"
    wb.save(actual)
    wb.close()
    assert compare(oracle, actual, "Result!A1:C4")["matched_cells"] == 0


def test_metadata_budget_stays_bounded(tmp_path):
    path = workbook(tmp_path / "oracle.xlsx")
    address = ",".join(["Result!A1:A100000"] * 11)
    with pytest.raises(ToolError, match="total answer cells"):
        compare(path, path, address)
