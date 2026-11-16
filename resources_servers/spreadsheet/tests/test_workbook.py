import os

import pytest


openpyxl = pytest.importorskip("openpyxl", reason="Run workbook tests on the authorized Modal CPU runtime")

from resources_servers.spreadsheet.schemas import TOOL_MODELS
from resources_servers.spreadsheet.service.engines import LibreOfficePool
from resources_servers.spreadsheet.service.workbook import compare, execute, records, split_ranges


def call(path, tool, **args):
    return execute(path, tool, TOOL_MODELS[tool].model_validate(args).model_dump(by_alias=True))


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "data.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Entries"
    wb.active.append(["Employee", "Hours"])
    wb.active.append(["Alice", 3])
    wb.active.append(["Bob", 5])
    wb.create_sheet("Summary")
    wb.save(path)
    return path


def test_multiple_sheets_fill_broadcast_and_formula_translation(book):
    call(book, "fill_formula", sheet_name="Summary", start_cell="A1", end_row=2, formula_template="=Entries!B2")
    wb = openpyxl.load_workbook(book)
    assert wb["Summary"]["A1"].value == "=Entries!B2"
    assert wb["Summary"]["A2"].value == "=Entries!B3"
    wb.close()
    call(book, "write_range", sheet_name="Summary", range="B1:C2", data=[7, 8])
    assert records(book, ["Summary!B1:C2"])["ranges"][0]["cells"][2]["value"] == 7
    with pytest.raises(ValueError):
        call(book, "write_range", range="A1", data="=SUM(A2:A3)")


def test_exact_summary_and_truncation(book):
    result = records(book, ["Entries!A1:B300"])["ranges"][0]
    assert len(result["cells"]) == 400
    assert result["truncated"] and result["omitted_from"] == "A201"
    summary = records(book, ["Entries!A1:B300"], summary=True)["ranges"][0]
    assert summary["non_empty"] == 6
    assert summary["empty"] == 594


def test_sheet_names_commas_and_multiple_answer_regions():
    assert split_ranges("'Sales, East'!A1:B2,'O''Brien'!C3") == ["'Sales, East'!A1:B2", "'O''Brien'!C3"]


def test_delete_intervals_are_original_positions_and_nonoverlapping(book):
    call(book, "delete_rows", sheet_name="Entries", rows=["1", "3"])
    values = records(book, ["Entries!A1:B1"])["ranges"][0]["cells"]
    assert values[0]["value"] == "Alice"
    with pytest.raises(ValueError, match="Overlapping"):
        call(book, "delete_rows", rows=["1:3", "2:4"])


def test_oracle_cell_values_binary_reward_and_no_expected_value_leak(book, tmp_path):
    oracle = tmp_path / "target.xlsx"
    oracle.write_bytes(book.read_bytes())
    assert compare(oracle, book, "Entries!A1:B3")["reward"] == 1
    call(book, "write_range", sheet_name="Entries", range="B3", data=6)
    result = compare(oracle, book, "Entries!A1:B3")
    assert result["reward"] == 0 and result["matched_cells"] == 5
    assert "Bob" not in str(result)


def test_manage_sheet_and_format(book):
    call(book, "manage_sheet", action="copy", sheet_name="Entries", new_name="Copy", index=0)
    call(book, "format_range", sheet_name="Copy", range="A1:B1", bold=True, fill_color="yellow")
    wb = openpyxl.load_workbook(book)
    assert wb.sheetnames[0] == "Copy"
    assert wb["Copy"]["A1"].font.bold
    assert wb["Copy"]["A1"].fill.fgColor.rgb == "FFFFFF00"
    wb.close()


@pytest.mark.skipif(not os.environ.get("SPREADSHEET_TEST_REAL_LO"), reason="Requires real Linux LibreOffice/UNO")
def test_real_libreoffice_recalculates_cross_sheet_formulas(book, tmp_path):
    call(book, "fill_formula", sheet_name="Summary", start_cell="A1", end_row=2, formula_template="=Entries!B2*2")
    engine = LibreOfficePool(tmp_path / "office", timeout=60)
    try:
        engine.recalculate(book)
        wb = openpyxl.load_workbook(book, data_only=True)
        assert wb["Summary"]["A1"].value == 6
        assert wb["Summary"]["A2"].value == 10
        assert "LibreOffice" in engine.version
        wb.close()
    finally:
        engine.close()
