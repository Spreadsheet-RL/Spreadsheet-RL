from __future__ import annotations

import asyncio
import math
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from verl.utils.paths import normalize_workspace_id
from verl.utils.rollout_trace import rollout_trace_op

from .base_tool import BaseTool
from .clear_range import (
    _EXCEL_MAX_COLS,
    _EXCEL_MAX_ROWS,
    _acquire_lockfile,
    _format_a1_address,
    _get_max_response_chars,
    _json_dumps_compact,
    _normalize_cell_range,
    _normalize_sheet_name,
    _quote_sheet_name_for_a1,
    _resolve_target_worksheet,
    _resolve_workspace_file,
    _sample_indices,
    _sanitize_relpath,
    _scan_zip_metadata,
    _split_sheet_cell_range,
    _to_jsonable_excel_value,
)
from .formula_fill_core import normalize_formula_for_excel
from .recalculate import _file_signature
from .schemas import OpenAIFunctionToolSchema, ToolResponse

MAX_CELL_TEXT_CHARS = 32_767


class WriteRangeError(RuntimeError):
    def __init__(self, message: str, error: str, payload: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.error = error
        self.payload = payload or {}


def _error_payload(error: str, message: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "status": "error",
        "error": error,
        "message": message,
        "truncated": False,
    }
    payload.update(extra)
    return payload


def _error_response(
    error: str,
    message: str,
    *,
    max_response_chars: int,
    metrics: Optional[dict[str, Any]] = None,
    **extra: Any,
) -> tuple[ToolResponse, float, dict]:
    payload = _error_payload(error, message, **extra)
    text = _truncate_payload_to_max_chars(payload, max_response_chars)
    out_metrics = {"status": "error", "error": error}
    if metrics:
        out_metrics.update(metrics)
    out_metrics["truncated"] = bool(payload.get("truncated"))
    return ToolResponse(text=text), 0.0, out_metrics


def _trim_list_field(payload: dict[str, Any], field: str, *, head: int, tail: int) -> None:
    values = payload.get(field)
    if not isinstance(values, list) or len(values) <= head + tail:
        return
    payload[field] = values[:head] + (values[-tail:] if tail > 0 else [])


def _truncate_payload_to_max_chars(payload: dict[str, Any], max_chars: int) -> str:
    payload.setdefault("truncated", False)
    response_text = _json_dumps_compact(payload)
    if len(response_text) <= max_chars:
        return response_text

    payload["truncated"] = True
    for head, tail in ((3, 3), (2, 2), (1, 1), (1, 0), (0, 0)):
        _trim_list_field(payload, "samples", head=head, tail=tail)
        _trim_list_field(payload, "overwritten_samples", head=head, tail=tail)
        response_text = _json_dumps_compact(payload)
        if len(response_text) <= max_chars:
            return response_text

    minimal_payload = {
        "status": payload.get("status"),
        "error": payload.get("error"),
        "file": payload.get("file"),
        "sheet": payload.get("sheet"),
        "written_range": payload.get("written_range"),
        "written_cells": payload.get("written_cells"),
        "overwritten_cells": payload.get("overwritten_cells"),
        "truncated": True,
    }
    response_text = _json_dumps_compact(minimal_payload)
    if len(response_text) <= max_chars:
        return response_text

    return _json_dumps_compact(
        {
            "status": payload.get("status"),
            "error": payload.get("error"),
            "written_cells": payload.get("written_cells"),
            "overwritten_cells": payload.get("overwritten_cells"),
            "truncated": True,
        }
    )


def _grid_shape(grid: Any, *, field_name: str) -> Optional[tuple[int, int]]:
    if grid is None:
        return None
    if not isinstance(grid, list) or not grid:
        raise WriteRangeError(f"{field_name} must be a non-empty 2D array", "invalid_dimensions")

    col_count: Optional[int] = None
    for row_idx, row in enumerate(grid, start=1):
        if not isinstance(row, list) or not row:
            raise WriteRangeError(f"{field_name}[{row_idx}] must be a non-empty array", "invalid_dimensions")
        if col_count is None:
            col_count = len(row)
        elif len(row) != col_count:
            raise WriteRangeError(f"{field_name} rows must all have the same length", "invalid_dimensions")

    assert col_count is not None
    return len(grid), col_count


def _validate_value(value: Any, *, row: int, col: int) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise WriteRangeError(f"values[{row}][{col}] must be finite", "invalid_values")
    if isinstance(value, str):
        if len(value) <= MAX_CELL_TEXT_CHARS:
            return
        raise WriteRangeError(
            f"values[{row}][{col}] is too long (len={len(value)} > {MAX_CELL_TEXT_CHARS})",
            "invalid_values",
        )
    raise WriteRangeError(f"values[{row}][{col}] must be a JSON scalar", "invalid_values")


def _normalize_formula(value: Any, *, row: int, col: int, max_formula_chars: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WriteRangeError(f"formulas[{row}][{col}] must be a non-empty string or null", "invalid_formulas")
    formula = value.strip()
    if not formula.startswith("="):
        formula = "=" + formula
    formula = normalize_formula_for_excel(formula)
    if max_formula_chars is not None and len(formula) > max_formula_chars:
        raise WriteRangeError(
            f"formulas[{row}][{col}] is too long (len={len(formula)} > {max_formula_chars})",
            "invalid_formulas",
        )
    return formula


def _coerce_write_matrix(
    *,
    values: Any,
    formulas: Any,
    max_formula_chars: Optional[int],
) -> tuple[list[list[tuple[str, Any]]], int, int]:
    value_shape = _grid_shape(values, field_name="values")
    formula_shape = _grid_shape(formulas, field_name="formulas")
    if value_shape is None and formula_shape is None:
        raise WriteRangeError("provide values and/or formulas", "missing_values")
    if value_shape is not None and formula_shape is not None and value_shape != formula_shape:
        raise WriteRangeError("values and formulas dimensions must match", "invalid_dimensions")

    rows, cols = value_shape or formula_shape or (0, 0)
    matrix: list[list[tuple[str, Any]]] = []
    for row_idx in range(rows):
        matrix_row: list[tuple[str, Any]] = []
        for col_idx in range(cols):
            value = values[row_idx][col_idx] if value_shape is not None else None
            formula = _normalize_formula(
                formulas[row_idx][col_idx] if formula_shape is not None else None,
                row=row_idx + 1,
                col=col_idx + 1,
                max_formula_chars=max_formula_chars,
            )

            if formula_shape is not None and formula is None and value_shape is None:
                raise WriteRangeError(
                    f"formulas[{row_idx + 1}][{col_idx + 1}] must be a non-empty string",
                    "invalid_formulas",
                )
            if formula is not None:
                if value is not None:
                    raise WriteRangeError(
                        f"values and formulas both set at row {row_idx + 1}, column {col_idx + 1}",
                        "conflicting_cell_payload",
                    )
                matrix_row.append(("formula", formula))
            else:
                _validate_value(value, row=row_idx + 1, col=col_idx + 1)
                matrix_row.append(("value", value))
        matrix.append(matrix_row)

    return matrix, rows, cols


def _parse_cell_bounds(token: str) -> tuple[Optional[str], tuple[int, int, int, int], str]:
    try:
        from openpyxl.utils.cell import get_column_letter, range_boundaries
    except ImportError:
        raise RuntimeError("openpyxl is required to edit workbooks; install openpyxl>=3.1.5") from None

    parsed_sheet, range_part = _split_sheet_cell_range(token)
    cell_range = _normalize_cell_range(range_part)
    if not cell_range:
        raise WriteRangeError("range/start_cell is empty", "invalid_range")
    if ":" not in cell_range:
        cell_range_for_parse = f"{cell_range}:{cell_range}"
    else:
        cell_range_for_parse = cell_range

    try:
        min_col, min_row, max_col, max_row = range_boundaries(cell_range_for_parse)
    except Exception as exc:
        raise WriteRangeError(f"failed to parse range boundaries: {exc}", "invalid_range") from None

    if None in (min_col, min_row, max_col, max_row):
        raise WriteRangeError("range must be a cell or rectangular cell range", "invalid_range")
    min_col = int(min_col)
    min_row = int(min_row)
    max_col = int(max_col)
    max_row = int(max_row)
    if min_col > max_col:
        min_col, max_col = max_col, min_col
    if min_row > max_row:
        min_row, max_row = max_row, min_row
    if min_col < 1 or min_row < 1 or max_col > _EXCEL_MAX_COLS or max_row > _EXCEL_MAX_ROWS:
        raise WriteRangeError("range is out of bounds", "invalid_range")

    normalized = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}"
    if min_col == max_col and min_row == max_row:
        normalized = f"{get_column_letter(min_col)}{min_row}"
    return parsed_sheet, (min_col, min_row, max_col, max_row), normalized


def _resolve_bounds(
    *,
    range_token: Optional[str],
    start_cell: Optional[str],
    sheet_name: Optional[str],
    rows: int,
    cols: int,
) -> tuple[Optional[str], tuple[int, int, int, int], str]:
    if range_token and start_cell:
        raise WriteRangeError("provide either range or start_cell, not both", "invalid_range")
    if range_token:
        parsed_sheet, bounds, normalized = _parse_cell_bounds(range_token)
    elif start_cell:
        parsed_sheet, bounds, normalized = _parse_cell_bounds(start_cell)
        min_col, min_row, max_col, max_row = bounds
        if min_col != max_col or min_row != max_row:
            raise WriteRangeError("start_cell must be a single A1 cell", "invalid_start_cell")
        max_col = min_col + cols - 1
        max_row = min_row + rows - 1
        if max_col > _EXCEL_MAX_COLS or max_row > _EXCEL_MAX_ROWS:
            raise WriteRangeError("target range is out of bounds", "invalid_range")
        try:
            from openpyxl.utils.cell import get_column_letter
        except ImportError:
            raise RuntimeError("openpyxl is required to edit workbooks; install openpyxl>=3.1.5") from None
        normalized = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}"
        if rows == 1 and cols == 1:
            normalized = f"{get_column_letter(min_col)}{min_row}"
        bounds = (min_col, min_row, max_col, max_row)
    else:
        raise WriteRangeError("range or start_cell is required", "invalid_range")

    if sheet_name is not None and parsed_sheet is not None and parsed_sheet.casefold() != sheet_name.casefold():
        raise WriteRangeError(
            f"sheet_name {sheet_name!r} does not match range sheet {parsed_sheet!r}", "sheet_mismatch"
        )
    requested_sheet = parsed_sheet or sheet_name
    return requested_sheet, bounds, normalized


def _sample_coords(*, min_col: int, min_row: int, rows: int, cols: int) -> list[tuple[int, int]]:
    if rows <= 1 and cols <= 1:
        return [(min_row, min_col)]
    if rows == 1:
        return [(min_row, min_col + idx) for idx in _sample_indices(cols, head=3, tail=3)]
    if cols == 1:
        return [(min_row + idx, min_col) for idx in _sample_indices(rows, head=3, tail=3)]
    sample_rows = [min_row + idx for idx in _sample_indices(rows, head=2, tail=2)]
    sample_cols = [min_col + idx for idx in _sample_indices(cols, head=2, tail=2)]
    return [(row_num, col_num) for row_num in sample_rows for col_num in sample_cols]


def _cell_snapshot(*, sheet_name: str, row: int, col: int, raw: Any, data_type: Any) -> dict[str, Any]:
    formula = None
    value = raw
    if data_type == "f" and isinstance(raw, str) and raw:
        formula = raw if raw.startswith("=") else f"={raw}"
        value = None
    return {
        "address": _format_a1_address(sheet_name=sheet_name, row=row, col=col),
        "formula": formula,
        "value": _to_jsonable_excel_value(value),
    }


def _copy_workbook_to_temp(file_path: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.",
        suffix=file_path.suffix,
        dir=str(file_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as dst:
            fd = -1
            with file_path.open("rb") as src:
                shutil.copyfileobj(src, dst)
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


def _validate_saved_workbook(file_path: Path) -> None:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError("openpyxl is required to edit workbooks; install openpyxl>=3.1.5") from None

    zip_error = _scan_zip_metadata(
        file_path,
        max_members=50_000,
        max_total_uncompressed_bytes=512 * 1024 * 1024,
        max_member_uncompressed_bytes=128 * 1024 * 1024,
        max_ratio=200.0,
    )
    if zip_error:
        raise RuntimeError(f"saved workbook rejected by zip safety checks: {zip_error}")

    wb = None
    try:
        wb = load_workbook(filename=str(file_path), data_only=False, read_only=True, keep_links=False)
        worksheets = getattr(wb, "worksheets", None) or []
        if not worksheets:
            raise RuntimeError("saved workbook has no worksheets")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"saved workbook failed load validation: {exc}") from None
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass


def _write_range_in_workbook(
    *,
    file_path: Path,
    sheet_name: Optional[str],
    range_token: Optional[str],
    start_cell: Optional[str],
    matrix: list[list[tuple[str, Any]]],
    rows: int,
    cols: int,
    allow_overwrite: bool,
    lock_timeout_s: float,
    max_write_cells: int,
    max_overwrite_samples: int,
) -> dict[str, Any]:
    try:
        from openpyxl import load_workbook
        from openpyxl.cell.cell import MergedCell
    except ImportError:
        raise RuntimeError("openpyxl is required to edit workbooks; install openpyxl>=3.1.5") from None

    target_cells = rows * cols
    if max_write_cells > 0 and target_cells > max_write_cells:
        raise WriteRangeError(
            f"requested range is too large (cells={target_cells} > {max_write_cells})", "range_too_large"
        )

    lock_file = _acquire_lockfile(file_path.with_suffix(file_path.suffix + ".lock"), timeout_s=lock_timeout_s)
    wb = None
    tmp_path: Optional[Path] = None
    try:
        zip_error = _scan_zip_metadata(
            file_path,
            max_members=50_000,
            max_total_uncompressed_bytes=512 * 1024 * 1024,
            max_member_uncompressed_bytes=128 * 1024 * 1024,
            max_ratio=200.0,
        )
        if zip_error:
            raise RuntimeError(f"workbook rejected by zip safety checks: {zip_error}")

        try:
            orig_mode = stat.S_IMODE(file_path.stat().st_mode)
            expected_sig = _file_signature(file_path)
        except OSError as exc:
            raise RuntimeError(f"failed to stat workbook: {exc}") from None

        tmp_path = _copy_workbook_to_temp(file_path)
        try:
            wb = load_workbook(filename=str(tmp_path), keep_vba=False, data_only=False, keep_links=True)
        except Exception as exc:
            raise RuntimeError(f"failed to load workbook: {exc}") from None

        worksheets = getattr(wb, "worksheets", None) or []
        if not worksheets:
            raise RuntimeError("workbook has no worksheets")

        requested_sheet, bounds, normalized_range = _resolve_bounds(
            range_token=range_token,
            start_cell=start_cell,
            sheet_name=sheet_name,
            rows=rows,
            cols=cols,
        )
        ws = _resolve_target_worksheet(wb, requested_sheet) if requested_sheet else worksheets[0]
        resolved_sheet_name = getattr(ws, "title", requested_sheet) or requested_sheet or ""
        sheet_ref = _quote_sheet_name_for_a1(resolved_sheet_name)
        min_col, min_row, max_col, max_row = bounds
        range_rows = max_row - min_row + 1
        range_cols = max_col - min_col + 1
        if range_rows != rows or range_cols != cols:
            raise WriteRangeError(
                f"payload dimensions {rows}x{cols} do not match target range {range_rows}x{range_cols}",
                "invalid_dimensions",
            )

        overwritten_cells = 0
        overwritten_samples: list[dict[str, Any]] = []
        max_overwrite_samples = max(0, int(max_overwrite_samples))
        for row_offset in range(rows):
            for col_offset in range(cols):
                row_num = min_row + row_offset
                col_num = min_col + col_offset
                cell = ws.cell(row=row_num, column=col_num)
                if isinstance(cell, MergedCell):
                    continue
                current = getattr(cell, "value", None)
                if current is None:
                    continue
                overwritten_cells += 1
                if len(overwritten_samples) < max_overwrite_samples:
                    overwritten_samples.append(
                        _cell_snapshot(
                            sheet_name=resolved_sheet_name,
                            row=row_num,
                            col=col_num,
                            raw=current,
                            data_type=getattr(cell, "data_type", None),
                        )
                    )

        written_range = f"{sheet_ref}!{normalized_range}"
        if overwritten_cells and not allow_overwrite:
            raise WriteRangeError(
                f"target range contains {overwritten_cells} non-empty cells and allow_overwrite is false",
                "overwrite_blocked",
                {
                    "file": file_path.name,
                    "sheet": resolved_sheet_name,
                    "written_range": written_range,
                    "target_cells": target_cells,
                    "written_cells": 0,
                    "overwritten_cells": overwritten_cells,
                    "overwritten_samples": overwritten_samples,
                },
            )

        skipped_merged_cells = 0
        written_lookup: dict[tuple[int, int], tuple[str, Any]] = {}
        for row_offset, matrix_row in enumerate(matrix):
            for col_offset, (kind, value) in enumerate(matrix_row):
                row_num = min_row + row_offset
                col_num = min_col + col_offset
                cell = ws.cell(row=row_num, column=col_num)
                if isinstance(cell, MergedCell):
                    skipped_merged_cells += 1
                    continue
                cell.value = value
                if kind == "value" and isinstance(value, str) and value.startswith("="):
                    cell.data_type = "s"
                written_lookup[(row_num, col_num)] = (kind, value)

        samples = []
        for row_num, col_num in _sample_coords(min_col=min_col, min_row=min_row, rows=rows, cols=cols):
            sample = written_lookup.get((row_num, col_num))
            if sample is None:
                continue
            kind, value = sample
            samples.append(
                {
                    "address": _format_a1_address(sheet_name=resolved_sheet_name, row=row_num, col=col_num),
                    "formula": value if kind == "formula" else None,
                    "value": None if kind == "formula" else _to_jsonable_excel_value(value),
                }
            )
        if not samples and written_lookup:
            for (row_num, col_num), (kind, value) in list(written_lookup.items())[:4]:
                samples.append(
                    {
                        "address": _format_a1_address(sheet_name=resolved_sheet_name, row=row_num, col=col_num),
                        "formula": value if kind == "formula" else None,
                        "value": None if kind == "formula" else _to_jsonable_excel_value(value),
                    }
                )

        wb.save(str(tmp_path))
        try:
            os.chmod(tmp_path, orig_mode)
        except OSError:
            pass
        try:
            wb.close()
        except Exception:
            pass
        wb = None

        _validate_saved_workbook(tmp_path)

        try:
            current_sig = _file_signature(file_path)
        except OSError as exc:
            raise RuntimeError(f"failed to stat workbook before writeback: {exc}") from None
        if current_sig != expected_sig:
            raise RuntimeError("workbook changed since write request; aborting writeback")

        os.replace(tmp_path, file_path)
        tmp_path = None
        written_cells = len(written_lookup)
        status = "partial_success" if skipped_merged_cells else "success"
        payload = {
            "status": status,
            "sheet": resolved_sheet_name,
            "written_range": written_range,
            "target_cells": target_cells,
            "written_cells": written_cells,
            "overwritten_cells": overwritten_cells,
            "overwritten_samples": overwritten_samples,
            "samples": samples,
            "recalculated": False,
            "truncated": False,
        }
        if skipped_merged_cells:
            payload["skipped_merged_cells"] = skipped_merged_cells
        return payload
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass
        try:
            lock_file.close()
        except Exception:
            pass


class WriteRangeTool(BaseTool):
    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict[str, Any]] = {}
        max_file_size_mb_raw = config.get("max_file_size_mb", 100)
        if max_file_size_mb_raw is None:
            self.max_file_size_bytes = None
        else:
            try:
                max_file_size_mb = int(max_file_size_mb_raw)
            except (TypeError, ValueError):
                max_file_size_mb = 100
            self.max_file_size_bytes = max_file_size_mb * 1024 * 1024 if max_file_size_mb > 0 else None

        lock_timeout_s_raw = config.get("lock_timeout_s", 30)
        try:
            lock_timeout_s = float(lock_timeout_s_raw)
        except (TypeError, ValueError):
            lock_timeout_s = 30.0
        self.lock_timeout_s = max(0.0, lock_timeout_s)
        self.max_response_chars = _get_max_response_chars(config)

        max_write_cells_raw = config.get(
            "max_write_cells", os.environ.get("SHEET_ARENA_WRITE_RANGE_MAX_CELLS", "50000")
        )
        try:
            self.max_write_cells = max(0, int(max_write_cells_raw))
        except (TypeError, ValueError):
            self.max_write_cells = 50_000

        max_formula_chars_raw = config.get("max_formula_chars", 8192)
        if max_formula_chars_raw is None:
            self.max_formula_chars = None
        else:
            try:
                max_formula_chars = int(max_formula_chars_raw)
            except (TypeError, ValueError):
                max_formula_chars = 8192
            self.max_formula_chars = max_formula_chars if max_formula_chars > 0 else None

        max_overwrite_samples_raw = config.get("max_overwrite_samples", 10)
        try:
            self.max_overwrite_samples = max(0, int(max_overwrite_samples_raw))
        except (TypeError, ValueError):
            self.max_overwrite_samples = 10

    async def create(self, instance_id: Optional[str] = None, **kwargs) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = uuid.uuid4().hex
        self._instance_dict[instance_id] = {}
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs) -> tuple[ToolResponse, float, dict]:
        raw_path = parameters.get("path")
        if raw_path is None or (isinstance(raw_path, str) and not raw_path.strip()):
            relpath = Path("data.xlsx")
        else:
            relpath = _sanitize_relpath(raw_path)
            if relpath is None:
                return _error_response("invalid_path", "invalid path", max_response_chars=self.max_response_chars)
        if relpath.suffix.lower() != ".xlsx":
            return _error_response(
                "invalid_path",
                "only .xlsx workbooks are supported",
                max_response_chars=self.max_response_chars,
            )

        sheet_name_raw = parameters.get("sheet_name")
        sheet_name = None
        if sheet_name_raw is not None:
            if not isinstance(sheet_name_raw, str) or not sheet_name_raw.strip():
                return _error_response(
                    "invalid_sheet_name",
                    "sheet_name must be a non-empty string",
                    max_response_chars=self.max_response_chars,
                )
            sheet_name = _normalize_sheet_name(sheet_name_raw)
            if not sheet_name:
                return _error_response(
                    "invalid_sheet_name",
                    "sheet_name is empty after normalization",
                    max_response_chars=self.max_response_chars,
                )

        range_raw = parameters.get("range")
        if range_raw is not None and (not isinstance(range_raw, str) or not range_raw.strip()):
            return _error_response(
                "invalid_range", "range must be a non-empty string", max_response_chars=self.max_response_chars
            )
        start_cell_raw = parameters.get("start_cell")
        if start_cell_raw is not None and (not isinstance(start_cell_raw, str) or not start_cell_raw.strip()):
            return _error_response(
                "invalid_start_cell",
                "start_cell must be a non-empty string",
                max_response_chars=self.max_response_chars,
            )

        allow_overwrite_raw = parameters.get("allow_overwrite", True)
        if not isinstance(allow_overwrite_raw, bool):
            return _error_response(
                "invalid_allow_overwrite",
                "allow_overwrite must be a boolean",
                max_response_chars=self.max_response_chars,
            )

        try:
            matrix, rows, cols = _coerce_write_matrix(
                values=parameters.get("values"),
                formulas=parameters.get("formulas"),
                max_formula_chars=self.max_formula_chars,
            )
        except WriteRangeError as exc:
            return _error_response(exc.error, str(exc), max_response_chars=self.max_response_chars, **exc.payload)

        workspace_id = normalize_workspace_id(kwargs.get("workspace_id"))
        if workspace_id is None:
            return _error_response(
                "missing_workspace_id",
                "workspace_id is missing/invalid",
                max_response_chars=self.max_response_chars,
            )
        file_path = _resolve_workspace_file(workspace_id=workspace_id, relpath=relpath)
        if file_path is None:
            return _error_response(
                "file_not_found",
                f"file not found: {relpath}",
                max_response_chars=self.max_response_chars,
                file=str(relpath),
            )
        if self.max_file_size_bytes is not None:
            try:
                file_size = file_path.stat().st_size
            except OSError as exc:
                return _error_response(
                    "stat_failed", f"failed to stat file: {exc}", max_response_chars=self.max_response_chars
                )
            if file_size > self.max_file_size_bytes:
                max_mb = self.max_file_size_bytes // (1024 * 1024)
                actual_mb = file_size / (1024 * 1024)
                return _error_response(
                    "file_too_large",
                    f"workbook is too large ({actual_mb:.1f}MB > {max_mb}MB)",
                    max_response_chars=self.max_response_chars,
                    file=str(relpath),
                )

        try:
            payload = await asyncio.to_thread(
                _write_range_in_workbook,
                file_path=file_path,
                sheet_name=sheet_name,
                range_token=range_raw.strip() if isinstance(range_raw, str) else None,
                start_cell=start_cell_raw.strip() if isinstance(start_cell_raw, str) else None,
                matrix=matrix,
                rows=rows,
                cols=cols,
                allow_overwrite=allow_overwrite_raw,
                lock_timeout_s=self.lock_timeout_s,
                max_write_cells=self.max_write_cells,
                max_overwrite_samples=self.max_overwrite_samples,
            )
        except asyncio.CancelledError:
            raise
        except WriteRangeError as exc:
            payload = _error_payload(exc.error, str(exc), **exc.payload)
            payload["file"] = str(relpath)
            response_text = _truncate_payload_to_max_chars(payload, self.max_response_chars)
            return (
                ToolResponse(text=response_text),
                0.0,
                {
                    "status": "error",
                    "error": exc.error,
                    "file": str(relpath),
                    "written_cells": int(payload.get("written_cells") or 0),
                    "overwritten_cells": int(payload.get("overwritten_cells") or 0),
                    "truncated": bool(payload.get("truncated")),
                },
            )
        except Exception as exc:
            return _error_response(
                "write_failed",
                f"failed to write range: {exc}",
                max_response_chars=self.max_response_chars,
                file=str(relpath),
            )

        payload["file"] = str(relpath)
        response_text = _truncate_payload_to_max_chars(payload, self.max_response_chars)
        samples_final = payload.get("samples")
        overwritten_samples_final = payload.get("overwritten_samples")
        metrics = {
            "status": payload.get("status", "success"),
            "file": str(relpath),
            "written_cells": payload["written_cells"],
            "overwritten_cells": payload["overwritten_cells"],
            "skipped_merged_cells": int(payload.get("skipped_merged_cells") or 0),
            "sample_cells": len(samples_final) if isinstance(samples_final, list) else 0,
            "overwritten_sample_cells": len(overwritten_samples_final)
            if isinstance(overwritten_samples_final, list)
            else 0,
            "recalculated": False,
            "truncated": bool(payload.get("truncated")),
        }
        return ToolResponse(text=response_text), 0.0, metrics

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
