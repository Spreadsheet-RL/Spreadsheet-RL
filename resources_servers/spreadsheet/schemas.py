"""Shared tool contracts. This module has no Gym or spreadsheet-engine dependency."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkbookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: Literal["data.xlsx"] = "data.xlsx"


class SheetRequest(WorkbookRequest):
    sheet_name: str | None = Field(default=None, max_length=31)


class InspectRange(SheetRequest):
    range: str = Field(min_length=1, max_length=2048)
    mode: Literal["cells", "summary"] = "cells"
    include_details: bool = False


class FindCells(SheetRequest):
    query: str = Field(min_length=1, max_length=512)
    range: str | None = Field(default=None, max_length=2048)
    match: Literal["contains", "equals", "prefix"] = "contains"
    search_in: Literal["values", "formulas", "both"] = "values"
    case_sensitive: bool = False
    include_values: bool = False
    return_: Literal["first", "all"] = Field(default="first", alias="return")
    max_results: int = Field(default=20, ge=1, le=400)


class WriteRange(SheetRequest):
    range: str = Field(min_length=1, max_length=2048)
    data: Any


class FillFormula(SheetRequest):
    start_cell: str = Field(min_length=1, max_length=2048)
    end_row: int | None = Field(default=None, ge=1, le=1048576)
    end_col: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}$")
    formula_template: str = Field(min_length=2, max_length=8192, pattern=r"^=")


class ClearRange(SheetRequest):
    range: str = Field(min_length=1, max_length=2048)


class FormatRange(ClearRange):
    fill_color: str | None = None
    font_color: str | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    number_format: str | None = Field(default=None, max_length=256)
    horizontal_alignment: (
        Literal["general", "left", "center", "right", "fill", "justify", "centerContinuous", "distributed"] | None
    ) = None
    vertical_alignment: Literal["top", "center", "bottom", "justify", "distributed"] | None = None
    wrap_text: bool | None = None
    shrink_to_fit: bool | None = None
    text_rotation: int | None = Field(default=None, ge=0, le=180)
    border_style: Literal["thin", "medium", "thick", "dashed", "dotted", "double", "hair"] | None = None
    border_color: str | None = None
    row_height: float | None = Field(default=None, gt=0, le=409)
    column_width: float | None = Field(default=None, gt=0, le=255)


class ManageSheet(WorkbookRequest):
    action: Literal["create", "rename", "delete", "copy", "move", "hide", "unhide"]
    sheet_name: str = Field(min_length=1, max_length=31)
    new_name: str | None = Field(default=None, min_length=1, max_length=31)
    index: int | None = Field(default=None, ge=0, le=199)


class DeleteRows(SheetRequest):
    rows: list[str] = Field(min_length=1, max_length=100)


class DeleteColumns(SheetRequest):
    columns: list[str] = Field(min_length=1, max_length=100)


class RecalculateAndRead(WorkbookRequest):
    cell_ranges: list[str] = Field(min_length=1, max_length=20)


class CodeInterpreter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=65536)


TOOL_MODELS = {
    "list_sheets": WorkbookRequest,
    "inspect_range": InspectRange,
    "find_cells": FindCells,
    "write_range": WriteRange,
    "fill_formula": FillFormula,
    "format_range": FormatRange,
    "manage_sheet": ManageSheet,
    "clear_range": ClearRange,
    "delete_rows": DeleteRows,
    "delete_columns": DeleteColumns,
    "recalculate_and_read": RecalculateAndRead,
    "code_interpreter": CodeInterpreter,
}
READ_ONLY = frozenset({"list_sheets", "inspect_range", "find_cells"})
RECALCULATE_ON_WRITE = frozenset({"fill_formula", "clear_range", "delete_rows", "delete_columns"})
TOOL_DESCRIPTIONS = {
    "list_sheets": "List workbook sheet names, order, visibility, and dimensions.",
    "inspect_range": "Inspect finite A1 ranges. cells returns addressed values/formulas; summary returns exact counts. Up to 400 cells are returned; use narrower ranges after truncation.",
    "find_cells": "Find text in a finite range or workbook. match supports contains, equals, and prefix (not regex).",
    "write_range": "Write static values. Formula strings are rejected; use fill_formula. null skips cells. A scalar/row/column can broadcast across a finite rectangle. Creates a missing sheet.",
    "fill_formula": "Fill one formula pattern from start_cell to end_row/end_col, translating relative references. LibreOffice recalculates before committing. Do not repeat successful edits.",
    "format_range": "Apply font, fill, alignment, borders, number format, row height, or column width to a finite A1 rectangle.",
    "manage_sheet": "Create, rename, delete, copy, move, hide, or unhide a sheet. index is the final 0-based position. Renaming does not rewrite formula references.",
    "clear_range": "Clear a finite A1 rectangle without shifting cells. LibreOffice recalculates before commit.",
    "delete_rows": "Delete numbered row intervals, e.g. ['2:3','7'], shifting cells up. LibreOffice recalculates before commit. References follow openpyxl deletion semantics; dependent formulas may need repair.",
    "delete_columns": "Delete column intervals, e.g. ['B:D','G'], shifting cells left. LibreOffice recalculates before commit. References follow openpyxl deletion semantics; dependent formulas may need repair.",
    "recalculate_and_read": "Recalculate all formulas with LibreOffice, persist updated values, then read the requested finite A1 ranges.",
    "code_interpreter": "Execute Python in a fresh isolated process. Work only on data.xlsx and save edits to that file. Only data.xlsx persists across calls; variables and auxiliary files do not. After changing formulas or their precedents call recalculate_and_read.",
}


def tool_schemas() -> list[dict]:
    """Responses API schemas; optional defaults deliberately use strict=false."""
    return [
        {
            "type": "function",
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "parameters": model.model_json_schema(by_alias=True),
            "strict": False,
        }
        for name, model in TOOL_MODELS.items()
    ]
