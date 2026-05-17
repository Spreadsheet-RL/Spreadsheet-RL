from __future__ import annotations

import tempfile
from pathlib import Path

import openpyxl

from async_reward_api.eval import (
    AnswerPositionError,
    _estimate_answer_position_cells,
    _get_gt_prepared_max_cells,
    compare_workbooks,
    parse_answer_position,
)


def _assert_equal(label: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{label}:\n  got={got!r}\n  expected={expected!r}")


def _expect_error(label: str, fn, /, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except AnswerPositionError:
        return
    raise AssertionError(f"Expected AnswerPositionError for {label!r}")


def _write_column_range_workbook(path: Path, *, include_extra_proc_row: bool) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = 1
    if include_extra_proc_row:
        ws["A2"] = 999
    wb.save(path)


def main() -> int:
    default_sheet = "__DEFAULT__"
    cases: list[tuple[str, list[tuple[str, str]]]] = [
        (
            # Intentionally includes a dangling apostrophe in the sheet name.
            "Pendency'!D7:D11,Pendency'!G7:G11",
            [("Pendency", "D7:D11"), ("Pendency", "G7:G11")],
        ),
        (
            "Sheet1'!C21:F30\"",
            [("Sheet1", "C21:F30")],
        ),
        (
            "'STATS 2025'!C6:C7,'STATS 2025'!E6:E7,'STATS 2025'!G6:G7,'STATS 2025'!I6:I7",
            [
                ("STATS 2025", "C6:C7"),
                ("STATS 2025", "E6:E7"),
                ("STATS 2025", "G6:G7"),
                ("STATS 2025", "I6:I7"),
            ],
        ),
        (
            "'Sheet'!B17:D17,'Sheet'!B30:D30,'Sheet'!B43:D43,'Sheet'!B56:D56",
            [
                ("Sheet", "B17:D17"),
                ("Sheet", "B30:D30"),
                ("Sheet", "B43:D43"),
                ("Sheet", "B56:D56"),
            ],
        ),
        (
            "'My, Sheet'!A1:B2,'Other'!C3:D4",
            [("My, Sheet", "A1:B2"), ("Other", "C3:D4")],
        ),
        (
            "  Sheet1 !  A1:B2  ,  Sheet2  ! C3 : D4  ",
            [("Sheet1", "A1:B2"), ("Sheet2", "C3:D4")],
        ),
        (
            "'My, Sheet''s Data'!A1:B2",
            [("My, Sheet's Data", "A1:B2")],
        ),
        (
            "'Test''s'!A1",
            [("Test's", "A1")],
        ),
        (
            "A1:A1",
            [(default_sheet, "A1:A1")],
        ),
        (
            # Full-width colon should be normalized.
            "G12：J15",
            [(default_sheet, "G12:J15")],
        ),
        (
            # Malformed quote placement around sheet tokens.
            "'RAWDATA!'A1:P6,'OUTPUT!'A1:P6'",
            [("RAWDATA", "A1:P6"), ("OUTPUT", "A1:P6")],
        ),
        (
            # Malformed duplicate-sheet fragment in range part.
            "'Received'!'Received!A1:G16'",
            [("Received", "A1:G16")],
        ),
        (
            # Column-only range should be accepted by parser.
            "Sheet3'!A:G,'Sheet4'!A:G",
            [("Sheet3", "A:G"), ("Sheet4", "A:G")],
        ),
        (
            # Malformed shorthand end-row form.
            "'Sheet1'!BD2:308",
            [("Sheet1", "BD2:BD308")],
        ),
        (
            # Curly quote variant should be normalized.
            "'Invoice List‘!A1:G10,'Invoice Items'!A1:G10",
            [("Invoice List", "A1:G10"), ("Invoice Items", "A1:G10")],
        ),
    ]

    for raw, expected in cases:
        got = parse_answer_position(raw, default_sheet_name=default_sheet)
        _assert_equal(raw, got, expected)

    _expect_error(
        "Sheet1!A1:B2:C3",
        parse_answer_position,
        "Sheet1!A1:B2:C3",
        default_sheet_name=default_sheet,
    )

    for raw in [
        "''!A1",
        "Sheet1!$A$1:$B$2",
        "  $a$1 : $b$2  ",
        "Sheet1!A1!B2",
        "'My Sheet',Sheet2!A1",
    ]:
        _expect_error(raw, parse_answer_position, raw, default_sheet_name=default_sheet)

    _assert_equal(
        "column-only range should bypass prepared GT cache",
        _estimate_answer_position_cells("Sheet1!A:G"),
        _get_gt_prepared_max_cells() + 1,
    )

    with tempfile.TemporaryDirectory(prefix="async_reward_api_answer_position_") as tmp:
        tmp_path = Path(tmp)
        gt_file = tmp_path / "gt.xlsx"
        proc_file = tmp_path / "proc.xlsx"
        _write_column_range_workbook(gt_file, include_extra_proc_row=False)
        _write_column_range_workbook(proc_file, include_extra_proc_row=True)
        ok, msg = compare_workbooks(gt_file, proc_file, "Sheet1!A:A")
        if not ok:
            raise AssertionError(f"column-only streaming changed GT-row-bound semantics: {msg}")

    print("OK: answer_position parsing looks good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
