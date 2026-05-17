from __future__ import annotations


def fill_formula(sheet, start_cell: str, end_row: int, end_col: str, formula_template: str) -> tuple[int, int]:
    """Fill a formula over an inclusive rectangular range.

    Returns (filled_cells, skipped_merged_cells).
    """
    try:
        from openpyxl.formula.translate import Translator
        from openpyxl.utils.cell import column_index_from_string, coordinate_from_string
        from openpyxl.cell.cell import MergedCell
    except ImportError:
        raise RuntimeError("openpyxl is required to fill formulas; install openpyxl>=3.1.5") from None

    normalized_start = start_cell.strip().upper().replace("$", "")
    start_col_letter, start_row = coordinate_from_string(normalized_start)
    start_col_idx = column_index_from_string(start_col_letter)
    if end_row < start_row:
        raise ValueError(f"end_row ({end_row}) must be >= start_cell row ({start_row})")

    end_col_norm = str(end_col or "").strip().upper().replace("$", "")
    if not end_col_norm:
        raise ValueError("end_col is empty")
    end_col_idx = column_index_from_string(end_col_norm)
    if end_col_idx < start_col_idx:
        raise ValueError(f"end_col ({end_col_norm}) must be >= start_cell col ({start_col_letter})")

    formula = str(formula_template or "").strip()
    if not formula:
        raise ValueError("formula_template is empty")
    if not formula.startswith("="):
        formula = "=" + formula

    translator = Translator(formula, origin=normalized_start)

    filled_cells = 0
    skipped_merged_cells = 0
    for row in range(start_row, end_row + 1):
        row_delta = row - start_row
        for col_idx in range(start_col_idx, end_col_idx + 1):
            col_delta = col_idx - start_col_idx
            cell = sheet.cell(row=row, column=col_idx)
            if isinstance(cell, MergedCell):
                if row_delta == 0 and col_delta == 0:
                    raise ValueError(f"start_cell {normalized_start!r} is a merged cell; use the top-left cell")
                skipped_merged_cells += 1
                continue

            if row_delta == 0 and col_delta == 0:
                cell.value = formula
            else:
                cell.value = translator.translate_formula(
                    row_delta=row_delta,
                    col_delta=col_delta,
                )
            filled_cells += 1

    return filled_cells, skipped_merged_cells
