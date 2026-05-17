from __future__ import annotations

import datetime
import os
import re
import sys
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException


def datetime_to_float(dt: datetime.datetime) -> float:
    excel_start_date = datetime.datetime(1899, 12, 30)
    delta = dt - excel_start_date
    return delta.days + delta.seconds / 86400.0


def transform_value(v):
    if isinstance(v, (int, float)):
        v = round(float(v), 2)
    elif isinstance(v, datetime.time):
        v = str(v)[:-3]
    elif isinstance(v, datetime.datetime):
        v = round(datetime_to_float(v), 0)
    elif isinstance(v, str):
        try:
            v = round(float(v), 2)
        except ValueError:
            pass
    return v


def compare_cell_value(v1, v2) -> bool:
    v1 = transform_value(v1)
    v2 = transform_value(v2)
    if (v1 == "" and v2 is None) or (v1 is None and v2 == ""):
        return True
    if (v1 == "" and v2 == "") or (v1 is None and v2 is None):
        return True
    if type(v1) is not type(v2):
        return False
    return v1 == v2


def col_num2name(n: int) -> str:
    name = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        name = chr(65 + remainder) + name
    return name


def col_name2num(name: str) -> int:
    num = 0
    for c in name:
        num = num * 26 + (ord(c) - ord("A") + 1)
    return num


_A1_CELL_RE = r"[A-Z]{1,3}[0-9]{1,7}"
_A1_RANGE_RE = re.compile(rf"^{_A1_CELL_RE}(?::{_A1_CELL_RE})?$")
_A1_CELL_COORD_RE = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})$")
_A1_COLUMN_RANGE_RE = re.compile(r"^[A-Z]{1,3}:[A-Z]{1,3}$")
_A1_RANGE_START_RE = re.compile(rf"^{_A1_CELL_RE}")


class AnswerPositionError(ValueError):
    pass


@dataclass(frozen=True)
class _PreparedRange:
    sheet_name: str
    cell_range: str
    min_col: int
    min_row: int
    max_col: int
    max_row: int
    gt_missing: bool
    expected_rows: tuple[tuple[object | None, ...], ...]


@dataclass(frozen=True)
class _PreparedGroundTruth:
    ranges: tuple[_PreparedRange, ...]


@dataclass(frozen=True)
class _GroundTruthCacheEntry:
    prepared: _PreparedGroundTruth | None
    error: str | None


_GT_CACHE_LOCK = threading.Lock()
_GT_CACHE: OrderedDict[tuple[str, str, int, int], _GroundTruthCacheEntry] = OrderedDict()


def _get_gt_cache_size() -> int:
    value = os.environ.get("REWARD_API_GT_CACHE_SIZE", "128")
    try:
        n = int(value)
        return min(max(0, n), 10000)
    except ValueError:
        return 128


def _get_gt_prepared_max_cells() -> int:
    value = os.environ.get("REWARD_API_GT_PREPARED_MAX_CELLS", "50000")
    try:
        n = int(value)
        return min(max(1000, n), 10_000_000)
    except ValueError:
        return 50000


def _estimate_answer_position_cells(answer_position: str) -> int | None:
    try:
        parsed_ranges = parse_answer_position(answer_position, default_sheet_name="Sheet1")
    except AnswerPositionError:
        return None

    total_cells = 0
    for _, cell_range in parsed_ranges:
        if _A1_COLUMN_RANGE_RE.fullmatch(cell_range):
            return _get_gt_prepared_max_cells() + 1
        try:
            min_col, min_row, max_col, max_row = _cell_range_boundaries(cell_range)
        except AnswerPositionError:
            return None
        total_cells += (max_col - min_col + 1) * (max_row - min_row + 1)
    return total_cells


def _extract_range_values(
    worksheet,
    *,
    min_col: int,
    min_row: int,
    max_col: int,
    max_row: int,
) -> tuple[tuple[object | None, ...], ...]:
    iter_rows = worksheet.iter_rows(
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
        values_only=True,
    )

    expected_cols = max_col - min_col + 1
    empty_row = (None,) * expected_cols
    rows: list[tuple[object | None, ...]] = []
    for _ in range(min_row, max_row + 1):
        try:
            row = next(iter_rows)
        except StopIteration:
            row = empty_row
        rows.append(tuple(row[offset] if offset < len(row) else None for offset in range(expected_cols)))
    return tuple(rows)


def _ground_truth_cache_key(gt_path: Path, answer_position: str) -> tuple[str, str, int, int]:
    stat = gt_path.stat()
    resolved = gt_path.resolve()
    return str(resolved), answer_position, int(stat.st_mtime_ns), int(stat.st_size)


def _prepare_ground_truth(gt_path: Path, answer_position: str) -> _GroundTruthCacheEntry:
    wb_gt = None
    try:
        try:
            wb_gt = openpyxl.load_workbook(
                filename=str(gt_path),
                data_only=True,
                read_only=True,
                keep_links=False,
            )
        except (OSError, zipfile.BadZipFile, InvalidFileException) as exc:
            return _GroundTruthCacheEntry(
                prepared=None,
                error=f"Failed to load ground truth workbook ({gt_path}): {exc}",
            )

        if not wb_gt.sheetnames:
            return _GroundTruthCacheEntry(
                prepared=None,
                error=f"Ground truth workbook has no worksheets: {gt_path}",
            )

        try:
            parsed_ranges = parse_answer_position(
                answer_position, default_sheet_name=wb_gt.sheetnames[0]
            )
        except AnswerPositionError as exc:
            return _GroundTruthCacheEntry(prepared=None, error=str(exc))

        prepared_ranges: list[_PreparedRange] = []
        for sheet_name, cell_range in parsed_ranges:
            ws_gt = wb_gt[sheet_name] if sheet_name in wb_gt else None
            max_row_hint = max(1, int(ws_gt.max_row or 1)) if ws_gt is not None else 1
            min_col, min_row, max_col, max_row = _cell_range_boundaries(
                cell_range,
                max_row_hint=max_row_hint,
            )
            if sheet_name not in wb_gt:
                prepared_ranges.append(
                    _PreparedRange(
                        sheet_name=sheet_name,
                        cell_range=cell_range,
                        min_col=min_col,
                        min_row=min_row,
                        max_col=max_col,
                        max_row=max_row,
                        gt_missing=True,
                        expected_rows=(),
                    )
                )
                continue

            expected_rows = _extract_range_values(
                ws_gt,
                min_col=min_col,
                min_row=min_row,
                max_col=max_col,
                max_row=max_row,
            )
            prepared_ranges.append(
                _PreparedRange(
                    sheet_name=sheet_name,
                    cell_range=cell_range,
                    min_col=min_col,
                    min_row=min_row,
                    max_col=max_col,
                    max_row=max_row,
                    gt_missing=False,
                    expected_rows=expected_rows,
                )
            )

        return _GroundTruthCacheEntry(
            prepared=_PreparedGroundTruth(ranges=tuple(prepared_ranges)),
            error=None,
        )
    finally:
        if wb_gt is not None:
            try:
                wb_gt.close()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[eval] wb_gt.close failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


def _get_or_prepare_ground_truth(gt_path: Path, answer_position: str) -> _GroundTruthCacheEntry:
    cache_size = _get_gt_cache_size()
    if cache_size <= 0:
        return _prepare_ground_truth(gt_path, answer_position)

    try:
        cache_key = _ground_truth_cache_key(gt_path, answer_position)
    except OSError as exc:
        return _GroundTruthCacheEntry(
            prepared=None,
            error=f"Failed to stat ground truth workbook ({gt_path}): {exc}",
        )

    with _GT_CACHE_LOCK:
        hit = _GT_CACHE.get(cache_key)
        if hit is not None:
            _GT_CACHE.move_to_end(cache_key)
            return hit

    prepared = _prepare_ground_truth(gt_path, answer_position)
    if prepared.error is not None:
        return prepared

    with _GT_CACHE_LOCK:
        _GT_CACHE[cache_key] = prepared
        _GT_CACHE.move_to_end(cache_key)
        while len(_GT_CACHE) > cache_size:
            _GT_CACHE.popitem(last=False)
    return prepared


def _split_answer_position(answer_position: str) -> list[str]:
    """
    Split an answer_position string into individual sheet/range tokens.

    We split on commas, but allow commas inside a quoted sheet name, e.g.:
      'My, Sheet'!A1:B2

    We intentionally only treat a leading quote (after optional whitespace) as the
    start of a quoted sheet name. This keeps us resilient to malformed inputs
    like: Sheet1'!A1:B2 (dangling apostrophe).
    """
    tokens: list[str] = []
    buf: list[str] = []
    in_quoted_sheet_name = False
    at_token_start = True

    def _flush_token() -> None:
        token = "".join(buf).strip()
        if token:
            tokens.append(token)

    i = 0
    while i < len(answer_position):
        ch = answer_position[i]

        # Allow benign wrappers (whitespace, double-quotes) before the token proper.
        # This helps with inputs like:  "'My, Sheet'!A1:B2,'Other'!C3:D4"
        if at_token_start and (ch.isspace() or ch == '"'):
            buf.append(ch)
            i += 1
            continue

        if at_token_start and ch == "'":
            in_quoted_sheet_name = True
            at_token_start = False
            buf.append(ch)
            i += 1
            continue

        at_token_start = False

        if in_quoted_sheet_name and ch == "'":
            # Excel escapes an apostrophe inside a quoted sheet name as ''.
            if i + 1 < len(answer_position) and answer_position[i + 1] == "'":
                buf.append("'")
                buf.append("'")
                i += 2
                continue

            # End quoted sheet name if this quote is followed by:
            #   - a sheet separator (!), or
            #   - a token separator (,), or
            #   - a direct range start (malformed form like: 'Sheet!'A1:B2), or
            #   - the end of the string
            lookahead = answer_position[i + 1 :].lstrip()
            if (
                not lookahead
                or lookahead.startswith(("!", ","))
                or _A1_RANGE_START_RE.match(lookahead) is not None
            ):
                in_quoted_sheet_name = False

            buf.append(ch)
            i += 1
            continue

        if ch == "," and not in_quoted_sheet_name:
            _flush_token()
            buf = []
            at_token_start = True
            i += 1
            continue

        buf.append(ch)
        i += 1

    _flush_token()
    return tokens


def _normalize_sheet_name(sheet_name: str) -> str:
    # Drop obvious wrapper quotes first (common when passing through JSON/CLI).
    name = sheet_name.strip(" \t\n\r\f\v\"")

    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        # Unwrap and unescape Excel-style '' -> '
        name = name[1:-1].replace("''", "'")
        return name.strip()

    # Handle malformed/dangling apostrophes like: Sheet1'!A1
    if name.startswith("'") and not name.endswith("'"):
        name = name[1:]
    elif name.endswith("'") and not name.startswith("'"):
        name = name[:-1]
    return name.strip()


def _normalize_cell_range(cell_range: str) -> str:
    # Drop obvious wrapper quotes first (common when passing through JSON/CLI).
    rng = cell_range.strip(" \t\n\r\f\v\"'")
    rng = rng.replace(" ", "")
    rng = rng.replace("：", ":")
    rng = rng.upper()

    # Recover malformed shorthand like "BD2:308" -> "BD2:BD308".
    match = re.fullmatch(r"^([A-Z]{1,3})([0-9]{1,7}):([0-9]{1,7})$", rng)
    if match is not None:
        col = match.group(1)
        start_row = match.group(2)
        end_row = match.group(3)
        return f"{col}{start_row}:{col}{end_row}"

    return rng


def _normalize_answer_position_text(answer_position: str) -> str:
    text = answer_position
    # Normalize common Unicode punctuation variants seen in dataset sources.
    text = text.replace("：", ":")
    text = text.replace("‘", "'").replace("’", "'")
    return text


def _is_supported_cell_range(cell_range: str) -> bool:
    return bool(_A1_RANGE_RE.fullmatch(cell_range) or _A1_COLUMN_RANGE_RE.fullmatch(cell_range))


def parse_answer_position(answer_position: str, *, default_sheet_name: str) -> list[tuple[str, str]]:
    """
    Parse answer_position into a list of (sheet_name, cell_range) pairs.

    Supported forms:
      - A1
      - A1:B2
      - Sheet1!A1
      - Sheet1!A1:B2
      - 'Sheet With Spaces'!A1:B2

    Multiple tokens can be separated by commas.
    """
    if not isinstance(answer_position, str) or not answer_position.strip():
        raise AnswerPositionError("answer_position is empty")

    answer_position = _normalize_answer_position_text(answer_position)
    tokens = _split_answer_position(answer_position)

    parsed: list[tuple[str, str]] = []
    for token in tokens:
        token = token.strip(" \t\n\r\f\v\"")
        if not token:
            continue

        if "!" in token:
            if token.count("!") > 1:
                sheet_part, range_part = token.split("!", 1)
                # Recover malformed pattern like "'Received'!'Received!A1:G16'".
                # We only salvage when extra "!" appears inside a quoted range fragment.
                if "!" in range_part and range_part.lstrip().startswith("'"):
                    range_part = range_part.rsplit("!", 1)[-1]
                else:
                    raise AnswerPositionError(
                        f"Malformed sheet reference (multiple '!' characters): {token!r}"
                    )
            else:
                sheet_part, range_part = token.split("!", 1)
            sheet_name = _normalize_sheet_name(sheet_part)
            if not sheet_name:
                raise AnswerPositionError(f"Empty sheet name in token: {token!r}")
        else:
            sheet_name = default_sheet_name
            range_part = token

        cell_range = _normalize_cell_range(range_part)
        if not _is_supported_cell_range(cell_range):
            raise AnswerPositionError(
                f"Invalid cell range: {range_part!r} (normalized: {cell_range!r})"
            )

        parsed.append((sheet_name, cell_range))

    if not parsed:
        raise AnswerPositionError("answer_position is empty")

    return parsed


def _cell_range_boundaries(
    cell_range: str,
    *,
    max_row_hint: int | None = None,
) -> tuple[int, int, int, int]:
    if _A1_COLUMN_RANGE_RE.fullmatch(cell_range):
        start_col_name, end_col_name = cell_range.split(":", 1)
        start_col = col_name2num(start_col_name)
        end_col = col_name2num(end_col_name)
        min_col, max_col = sorted((start_col, end_col))

        if max_row_hint is None:
            raise AnswerPositionError(
                f"Column-only range requires max_row_hint: {cell_range!r}"
            )
        max_row = max(1, int(max_row_hint))
        return min_col, 1, max_col, max_row

    if ":" in cell_range:
        start_cell, end_cell = cell_range.split(":", 1)
    else:
        start_cell, end_cell = cell_range, cell_range

    match_start = _A1_CELL_COORD_RE.fullmatch(start_cell)
    match_end = _A1_CELL_COORD_RE.fullmatch(end_cell)
    if match_start is None or match_end is None:
        raise AnswerPositionError(f"Invalid cell range: {cell_range!r}")

    start_col = col_name2num(match_start.group(1))
    start_row = int(match_start.group(2))
    end_col = col_name2num(match_end.group(1))
    end_row = int(match_end.group(2))

    min_col, max_col = sorted((start_col, end_col))
    min_row, max_row = sorted((start_row, end_row))
    return min_col, min_row, max_col, max_row


def _compare_proc_with_prepared(
    prepared: _PreparedGroundTruth,
    wb_proc,
) -> tuple[bool, str]:
    for item in prepared.ranges:
        sheet_name = item.sheet_name
        missing_in_gt = item.gt_missing
        missing_in_proc = sheet_name not in wb_proc
        if missing_in_gt or missing_in_proc:
            if missing_in_gt and missing_in_proc:
                return False, "Worksheet was not found in either workbook."
            if missing_in_gt:
                return False, "Worksheet was not found in the reference workbook."
            return False, "Worksheet was not found in your workbook."

        ws_proc = wb_proc[sheet_name]
        iter_proc = ws_proc.iter_rows(
            min_row=item.min_row,
            max_row=item.max_row,
            min_col=item.min_col,
            max_col=item.max_col,
            values_only=True,
        )
        expected_cols = item.max_col - item.min_col + 1
        empty_row = (None,) * expected_cols

        for row_offset, row_idx in enumerate(range(item.min_row, item.max_row + 1)):
            expected_row = item.expected_rows[row_offset] if row_offset < len(item.expected_rows) else empty_row
            try:
                proc_row = next(iter_proc)
            except StopIteration:
                proc_row = empty_row

            for offset in range(expected_cols):
                v_gt = expected_row[offset]
                v_proc = proc_row[offset] if offset < len(proc_row) else None
                if compare_cell_value(v_gt, v_proc):
                    continue
                col_idx = item.min_col + offset
                coord = f"{col_num2name(col_idx)}{row_idx}"
                msg = f"Value mismatch at {coord}: your workbook has {v_proc!r}"
                return False, msg
    return True, ""


def _compare_workbooks_streaming(gt_path: Path, proc_path: Path, answer_position: str) -> tuple[bool, str]:
    wb_gt = None
    wb_proc = None
    try:
        try:
            wb_gt = openpyxl.load_workbook(
                filename=str(gt_path),
                data_only=True,
                read_only=True,
                keep_links=False,
            )
        except (OSError, zipfile.BadZipFile, InvalidFileException) as exc:
            return False, f"Failed to load ground truth workbook ({gt_path}): {exc}"
        try:
            wb_proc = openpyxl.load_workbook(
                filename=str(proc_path),
                data_only=True,
                read_only=True,
                keep_links=False,
            )
        except (OSError, zipfile.BadZipFile, InvalidFileException) as exc:
            return False, f"Failed to load your processed workbook ({proc_path}): {exc}"

        if not wb_gt.sheetnames:
            return False, f"Ground truth workbook has no worksheets: {gt_path}"

        try:
            parsed_ranges = parse_answer_position(answer_position, default_sheet_name=wb_gt.sheetnames[0])
        except AnswerPositionError as exc:
            return False, str(exc)

        for sheet_name, cell_range in parsed_ranges:
            missing_in_gt = sheet_name not in wb_gt
            missing_in_proc = sheet_name not in wb_proc
            if missing_in_gt or missing_in_proc:
                if missing_in_gt and missing_in_proc:
                    return False, "Worksheet was not found in either workbook."
                if missing_in_gt:
                    return False, "Worksheet was not found in the reference workbook."
                return False, "Worksheet was not found in your workbook."

            ws_gt = wb_gt[sheet_name]
            ws_proc = wb_proc[sheet_name]
            max_row_hint = max(1, int(ws_gt.max_row or 1))
            if not _A1_COLUMN_RANGE_RE.fullmatch(cell_range):
                max_row_hint = max(max_row_hint, int(ws_proc.max_row or 1))
            min_col, min_row, max_col, max_row = _cell_range_boundaries(
                cell_range,
                max_row_hint=max_row_hint,
            )
            iter_gt = ws_gt.iter_rows(
                min_row=min_row,
                max_row=max_row,
                min_col=min_col,
                max_col=max_col,
                values_only=True,
            )
            iter_proc = ws_proc.iter_rows(
                min_row=min_row,
                max_row=max_row,
                min_col=min_col,
                max_col=max_col,
                values_only=True,
            )
            expected_cols = max_col - min_col + 1
            empty_row = (None,) * expected_cols

            for row_idx in range(min_row, max_row + 1):
                try:
                    gt_row = next(iter_gt)
                except StopIteration:
                    gt_row = empty_row
                try:
                    proc_row = next(iter_proc)
                except StopIteration:
                    proc_row = empty_row

                for offset in range(expected_cols):
                    v_gt = gt_row[offset] if offset < len(gt_row) else None
                    v_proc = proc_row[offset] if offset < len(proc_row) else None
                    if compare_cell_value(v_gt, v_proc):
                        continue
                    col_idx = min_col + offset
                    coord = f"{col_num2name(col_idx)}{row_idx}"
                    msg = f"Value mismatch at {coord}: your workbook has {v_proc!r}"
                    return False, msg

        return True, ""
    finally:
        if wb_proc is not None:
            try:
                wb_proc.close()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[eval] wb_proc.close failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        if wb_gt is not None:
            try:
                wb_gt.close()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[eval] wb_gt.close failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


def compare_workbooks(gt_file: str | Path, proc_file: str | Path, answer_position: str):
    gt_path = Path(gt_file)
    proc_path = Path(proc_file)

    if not gt_path.exists():
        return False, f"Ground truth file does not exist: {gt_path}"
    if not proc_path.exists():
        return False, f"Your processed file does not exist: {proc_path}"

    estimated_cells = _estimate_answer_position_cells(answer_position)
    max_prepared_cells = _get_gt_prepared_max_cells()
    if estimated_cells is not None and estimated_cells > max_prepared_cells:
        return _compare_workbooks_streaming(gt_path, proc_path, answer_position)

    gt_cache_entry = _get_or_prepare_ground_truth(gt_path, answer_position)
    if gt_cache_entry.error is not None:
        return False, gt_cache_entry.error
    if gt_cache_entry.prepared is None:
        return False, "ground truth preparation failed"

    wb_proc = None
    try:
        try:
            wb_proc = openpyxl.load_workbook(
                filename=str(proc_path),
                data_only=True,
                read_only=True,
                keep_links=False,
            )
        except (OSError, zipfile.BadZipFile, InvalidFileException) as exc:
            return False, f"Failed to load your processed workbook ({proc_path}): {exc}"

        return _compare_proc_with_prepared(gt_cache_entry.prepared, wb_proc)
    finally:
        if wb_proc is not None:
            try:
                wb_proc.close()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[eval] wb_proc.close failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


def compute_reward(gt_file: Path, proc_file: Path, answer_position: str) -> tuple[float, str]:
    ok, msg = compare_workbooks(gt_file, proc_file, answer_position)
    reward = 1.0 if ok else 0.0
    return reward, msg
