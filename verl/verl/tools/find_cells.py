from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from verl.utils.paths import get_spreadsheet_rl_data_root, normalize_workspace_id
from verl.utils.rollout_trace import rollout_trace_op

from .base_tool import BaseTool
from .schemas import OpenAIFunctionToolSchema, ToolResponse

_EXCEL_MAX_ROWS = 1_048_576
_EXCEL_MAX_COLS = 16_384
_SAFE_SHEET_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _get_max_scan_cells() -> int:
    value = os.environ.get("SPREADSHEET_RL_FIND_MAX_SCAN_CELLS", "200000").strip()
    try:
        n = int(value)
        return min(max(1, n), 10_000_000)
    except ValueError:
        return 200_000


def _get_max_response_chars(config: Optional[dict[str, Any]] = None) -> int:
    config_value = None
    if isinstance(config, dict):
        config_value = config.get("max_response_chars")
    for raw in (config_value, os.environ.get("SPREADSHEET_RL_TOOL_MAX_RESPONSE_CHARS", "").strip()):
        if raw is None:
            continue
        try:
            n = int(raw)
            return min(max(128, n), 1000)
        except (TypeError, ValueError):
            continue
    return 900


def _get_max_string_chars() -> int:
    raw = os.environ.get("SPREADSHEET_RL_TOOL_MAX_STRING_CHARS", "200").strip()
    try:
        n = int(raw)
        return min(max(16, n), 100_000)
    except ValueError:
        return 200


def _json_dumps_compact(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _truncate_str(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    return value[: max_chars - 3] + "..."


def _bounds_to_a1(*, min_col: int, min_row: int, max_col: int, max_row: int) -> str:
    try:
        from openpyxl.utils.cell import get_column_letter
    except ImportError:
        left = f"{min_col}{min_row}"
        right = f"{max_col}{max_row}"
    else:
        left = f"{get_column_letter(min_col)}{min_row}"
        right = f"{get_column_letter(max_col)}{max_row}"
    return left if left == right else f"{left}:{right}"


def _sanitize_relpath(value: Any) -> Optional[Path]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if "\0" in raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return None
    if any(part == ".." for part in p.parts):
        return None
    return p


def _normalize_sheet_name(value: str) -> str:
    name = value.strip(" \t\n\r\f\v\"")
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        name = name[1:-1].replace("''", "'")
        return name.strip()

    if name.startswith("'") and not name.endswith("'"):
        name = name[1:]
    elif name.endswith("'") and not name.startswith("'"):
        name = name[:-1]
    return name.strip()


def _normalize_cell_range(value: str) -> str:
    rng = value.strip(" \t\n\r\f\v\"'")
    rng = rng.replace(" ", "").replace("$", "")
    return rng.upper()


def _resolve_workspace_file(*, workspace_id: Optional[str], relpath: Path) -> Optional[Path]:
    if not workspace_id:
        return None

    workspace_base = get_spreadsheet_rl_data_root()
    workspaces_base = workspace_base / "_workspaces"
    try:
        if workspace_base.is_symlink():
            return None
        workspaces_base_resolved = workspaces_base.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    workspace_root = workspaces_base / workspace_id
    try:
        if workspace_root.is_symlink():
            return None
        workspace_root_resolved = workspace_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not workspace_root_resolved.is_relative_to(workspaces_base_resolved):
        return None

    rel_candidates = [relpath]
    if relpath.parts and relpath.parts[0] == "data" and len(relpath.parts) > 1:
        rel_candidates.append(Path(*relpath.parts[1:]))

    for rel_candidate in rel_candidates:
        candidate = workspace_root / rel_candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(workspace_root_resolved):
            continue

        if candidate.is_symlink():
            continue

        cursor = workspace_root
        safe = True
        for part in rel_candidate.parts:
            cursor = cursor / part
            try:
                if cursor.is_symlink():
                    safe = False
                    break
            except OSError:
                safe = False
                break
        if not safe:
            continue

        if resolved.is_file():
            return resolved
    return None


def _quote_sheet_name_for_a1(sheet_name: str) -> str:
    if _SAFE_SHEET_NAME_RE.fullmatch(sheet_name):
        return sheet_name
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def _format_a1_address(*, sheet_name: str, row: int, col: int) -> str:
    try:
        from openpyxl.utils.cell import get_column_letter
    except ImportError:
        col_letter = str(col)
    else:
        col_letter = get_column_letter(col)
    sheet_ref = _quote_sheet_name_for_a1(sheet_name)
    return f"{sheet_ref}!{col_letter}{row}"


def _to_jsonable_excel_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            return _truncate_str(value, _get_max_string_chars())
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return str(value)


def _resolve_target_worksheet(wb, requested_name: str):
    worksheets = getattr(wb, "worksheets", None) or []
    for ws in worksheets:
        if getattr(ws, "title", None) == requested_name:
            return ws

    requested_cf = requested_name.casefold()
    matches = [ws for ws in worksheets if getattr(ws, "title", "").casefold() == requested_cf]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = ", ".join(repr(getattr(ws, "title", "")) for ws in matches[:5])
        raise RuntimeError(f"ambiguous sheet name: {requested_name!r} matches {candidates}")

    sheetnames = getattr(wb, "sheetnames", None) or []
    if any(isinstance(name, str) and name.casefold() == requested_cf for name in sheetnames):
        raise RuntimeError(f"sheet is not a worksheet: {requested_name!r}")
    raise RuntimeError(f"sheet not found: {requested_name!r}")


def _scan_zip_metadata(
    file_path: Path,
    *,
    max_members: int,
    max_total_uncompressed_bytes: int,
    max_member_uncompressed_bytes: int,
    max_ratio: float,
) -> Optional[str]:
    try:
        import zipfile
    except ImportError:
        return None

    try:
        with zipfile.ZipFile(file_path) as zf:
            infos = zf.infolist()
            if len(infos) > max_members:
                return f"too many zip members (members={len(infos)}, max={max_members})"

            total_uncompressed = 0
            total_compressed = 0
            for info in infos:
                uncompressed = int(getattr(info, "file_size", 0) or 0)
                compressed = int(getattr(info, "compress_size", 0) or 0)

                total_uncompressed += uncompressed
                total_compressed += compressed

                if uncompressed > max_member_uncompressed_bytes:
                    return (
                        "zip member too large "
                        f"(name={getattr(info, 'filename', '?')!r}, bytes={uncompressed}, max={max_member_uncompressed_bytes})"
                    )
                if compressed > 0 and max_ratio > 0 and (uncompressed / compressed) > max_ratio:
                    return (
                        "zip member compression ratio too large "
                        f"(name={getattr(info, 'filename', '?')!r}, ratio={uncompressed / compressed:.1f}, max={max_ratio})"
                    )

                if total_uncompressed > max_total_uncompressed_bytes:
                    return (
                        "zip contents too large "
                        f"(total_bytes={total_uncompressed}, max={max_total_uncompressed_bytes})"
                    )

            if total_compressed > 0 and max_ratio > 0 and (total_uncompressed / total_compressed) > max_ratio:
                return (
                    "zip total compression ratio too large "
                    f"(ratio={total_uncompressed / total_compressed:.1f}, max={max_ratio})"
                )
    except zipfile.BadZipFile:
        return "invalid zip file"
    except Exception as exc:
        return f"failed to read zip metadata: {exc}"

    return None


def _normalize_match_mode(value: Any) -> str:
    if value is None:
        return "contains"
    if not isinstance(value, str):
        raise ValueError("match must be a string")
    mode = value.strip().lower()
    if mode in {"contains", "equals", "prefix", "regex"}:
        return mode
    raise ValueError("match must be one of: contains, equals, prefix, regex")


def _normalize_search_in(value: Any) -> str:
    if value is None:
        return "values"
    if not isinstance(value, str):
        raise ValueError("search_in must be a string")
    mode = value.strip().lower()
    if mode in {"values", "formulas", "both"}:
        return mode
    raise ValueError("search_in must be one of: values, formulas, both")


def _normalize_return_mode(value: Any) -> str:
    if value is None:
        return "first"
    if not isinstance(value, str):
        raise ValueError("return must be a string")
    mode = value.strip().lower()
    if mode in {"first", "all"}:
        return mode
    raise ValueError("return must be one of: first, all")


def _get_regex_haystack_chars() -> int:
    raw = os.environ.get("SPREADSHEET_RL_FIND_REGEX_HAYSTACK_CHARS", "2000").strip()
    try:
        n = int(raw)
        return min(max(64, n), 100_000)
    except ValueError:
        return 2000


def _get_max_regex_pattern_chars() -> int:
    raw = os.environ.get("SPREADSHEET_RL_FIND_REGEX_MAX_PATTERN_CHARS", "200").strip()
    try:
        n = int(raw)
        return min(max(16, n), 2000)
    except ValueError:
        return 200


def _validate_regex_pattern(pattern: str) -> Optional[str]:
    if not pattern:
        return "regex pattern is empty"

    in_class = False
    escaped = False
    for ch in pattern:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "[" and not in_class:
            in_class = True
            continue
        if ch == "]" and in_class:
            in_class = False
            continue
        if in_class:
            continue
        if ch in ("(", ")", "{", "}", "|", "+", "?"):
            return (
                "regex pattern uses unsupported constructs; supported: literal text, '.', '*', '^', '$', "
                "character classes '[...]', and escapes"
            )

    if escaped:
        return "regex pattern ends with a trailing backslash"
    if in_class:
        return "regex pattern has an unterminated character class"
    return None


def _find_cells_sync(
    *,
    file_path: Path,
    sheet_name: str,
    query: str,
    search_range: Optional[str],
    match_mode: str,
    search_in: str,
    case_sensitive: bool,
    return_mode: str,
    max_results: int,
    max_response_chars: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.cell import range_boundaries
    except ImportError:
        raise RuntimeError("openpyxl is required; install openpyxl>=3.1.5") from None

    if max_results < 1:
        max_results = 1

    wb_values = None
    wb_formula = None
    try:
        if search_in in {"values", "both"}:
            wb_values = load_workbook(filename=str(file_path), data_only=True, read_only=True, keep_links=False)
        if search_in in {"formulas", "both"}:
            wb_formula = load_workbook(filename=str(file_path), data_only=False, read_only=True, keep_links=False)

        wb_ref = wb_values or wb_formula
        if wb_ref is None:
            raise RuntimeError("invalid search_in mode")

        ws_ref = _resolve_target_worksheet(wb_ref, sheet_name)
        actual_sheet = getattr(ws_ref, "title", sheet_name) or sheet_name

        used_range = None
        try:
            used_range = ws_ref.calculate_dimension()
        except Exception:
            used_range = None
        if not isinstance(used_range, str) or not used_range.strip():
            try:
                dim = getattr(ws_ref, "dimensions", None)
            except Exception:
                dim = None
            used_range = dim.strip() if isinstance(dim, str) else ""
        if not used_range:
            try:
                max_row = int(getattr(ws_ref, "max_row", 1) or 1)
                max_col = int(getattr(ws_ref, "max_column", 1) or 1)
                max_row = min(max(1, max_row), _EXCEL_MAX_ROWS)
                max_col = min(max(1, max_col), _EXCEL_MAX_COLS)
                used_range = _bounds_to_a1(min_col=1, min_row=1, max_col=max_col, max_row=max_row)
            except Exception:
                used_range = "A1:A1"

        effective_range = search_range.strip() if isinstance(search_range, str) and search_range.strip() else used_range
        if "!" in effective_range:
            sheet_part, _, range_part = effective_range.rpartition("!")
            requested_sheet = _normalize_sheet_name(sheet_part)
            if not requested_sheet:
                raise RuntimeError(f"empty sheet name in range: {effective_range!r}")
            if requested_sheet.casefold() != actual_sheet.casefold():
                raise RuntimeError(f"range sheet {requested_sheet!r} does not match sheet_name {actual_sheet!r}")
            effective_range = range_part.strip()
        effective_range = _normalize_cell_range(effective_range)
        used_range = _normalize_cell_range(used_range)

        used_boundaries = range_boundaries(used_range if ":" in used_range else f"{used_range}:{used_range}")
        used_min_col, used_min_row, used_max_col, used_max_row = used_boundaries
        if used_min_col is None:
            used_min_col = 1
        if used_max_col is None:
            used_max_col = _EXCEL_MAX_COLS
        if used_min_row is None:
            used_min_row = 1
        if used_max_row is None:
            used_max_row = _EXCEL_MAX_ROWS
        used_min_col = int(used_min_col)
        used_min_row = int(used_min_row)
        used_max_col = int(used_max_col)
        used_max_row = int(used_max_row)

        req_boundaries = range_boundaries(effective_range if ":" in effective_range else f"{effective_range}:{effective_range}")
        req_min_col, req_min_row, req_max_col, req_max_row = req_boundaries
        if req_min_col is None:
            req_min_col = 1
        if req_max_col is None:
            req_max_col = _EXCEL_MAX_COLS
        if req_min_row is None:
            req_min_row = 1
        if req_max_row is None:
            req_max_row = _EXCEL_MAX_ROWS
        req_min_col = int(req_min_col)
        req_min_row = int(req_min_row)
        req_max_col = int(req_max_col)
        req_max_row = int(req_max_row)
        requested_cell_count = (req_max_col - req_min_col + 1) * (req_max_row - req_min_row + 1)

        min_col = max(req_min_col, used_min_col)
        min_row = max(req_min_row, used_min_row)
        max_col = min(req_max_col, used_max_col)
        max_row = min(req_max_row, used_max_row)
        scanned_range = None
        if max_col >= min_col and max_row >= min_row:
            scanned_range = _bounds_to_a1(min_col=min_col, min_row=min_row, max_col=max_col, max_row=max_row)
            candidate_cell_count = (max_col - min_col + 1) * (max_row - min_row + 1)
        else:
            candidate_cell_count = 0

        max_scan_cells = _get_max_scan_cells()

        regex = None
        if match_mode == "regex":
            flags = 0 if case_sensitive else re.IGNORECASE
            regex = re.compile(query, flags=flags)
        q = query if case_sensitive else query.casefold()

        regex_haystack_chars = _get_regex_haystack_chars()
        if match_mode == "regex":
            max_scan_cells = min(max_scan_cells, 10_000)
            regex_haystack_chars = min(regex_haystack_chars, 200)

        def matches_text(text: str) -> bool:
            if match_mode == "regex":
                if regex is None:
                    return False
                return regex.search(text[:regex_haystack_chars]) is not None
            hay = text if case_sensitive else text.casefold()
            if match_mode == "contains":
                return q in hay
            if match_mode == "equals":
                return hay == q
            if match_mode == "prefix":
                return hay.startswith(q)
            return False

        ws_values = None
        ws_formula = None
        if wb_values is not None:
            ws_values = _resolve_target_worksheet(wb_values, actual_sheet)
        if wb_formula is not None:
            ws_formula = _resolve_target_worksheet(wb_formula, actual_sheet)

        results: list[dict[str, Any]] = []
        truncated = False
        scan_truncated = False
        scanned = 0

        sheet_ref = _quote_sheet_name_for_a1(actual_sheet)
        range_out = scanned_range or effective_range
        requested_range_out = effective_range if scanned_range is not None and scanned_range != effective_range else None

        if candidate_cell_count <= 0:
            payload = {
                "status": "success",
                "file": str(file_path),
                "sheet": actual_sheet,
                "range": f"{sheet_ref}!{range_out}",
                "query": _to_jsonable_excel_value(query),
                "match": match_mode,
                "search_in": search_in,
                "case_sensitive": case_sensitive,
                "truncated": False,
                "scan_truncated": False,
                "matches": [],
            }
            if requested_range_out is not None:
                payload["requested_range"] = f"{sheet_ref}!{requested_range_out}"
            metrics = {
                "status": "success",
                "file": str(file_path),
                "sheet": actual_sheet,
                "scanned_cells": 0,
                "requested_cells": requested_cell_count,
                "candidate_cells": candidate_cell_count,
                "returned_matches": 0,
                "truncated": False,
                "scan_truncated": False,
            }
            return payload, metrics

        def iter_rows_values():
            if ws_values is None:
                return []
            return ws_values.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col)

        def iter_rows_formula():
            if ws_formula is None:
                return []
            return ws_formula.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col)

        if search_in == "both":
            row_iter = itertools.zip_longest(iter_rows_values(), iter_rows_formula(), fillvalue=())
        elif search_in == "values":
            row_iter = ((row, None) for row in iter_rows_values())
        else:
            row_iter = ((None, row) for row in iter_rows_formula())

        for r_idx, (row_vals, row_forms) in enumerate(row_iter, start=0):
            row_num = min_row + r_idx
            if row_vals is None:
                row_vals = []
            if row_forms is None:
                row_forms = []
            max_len = max(len(row_vals), len(row_forms))
            for c_idx in range(max_len):
                col_num = min_col + c_idx
                if scanned >= max_scan_cells:
                    scan_truncated = True
                    break
                scanned += 1

                value_obj = None
                if search_in in {"values", "both"} and c_idx < len(row_vals):
                    try:
                        value_obj = getattr(row_vals[c_idx], "value", None)
                    except Exception:
                        value_obj = None

                formula_text = None
                if search_in in {"formulas", "both"} and c_idx < len(row_forms):
                    try:
                        cell_f = row_forms[c_idx]
                        if getattr(cell_f, "data_type", None) == "f":
                            raw = getattr(cell_f, "value", None)
                            if isinstance(raw, str) and raw:
                                formula_text = raw if raw.startswith("=") else f"={raw}"
                    except Exception:
                        formula_text = None

                haystacks: list[str] = []
                if search_in in {"values", "both"}:
                    if value_obj is not None:
                        haystacks.append(str(value_obj))
                if search_in in {"formulas", "both"}:
                    if formula_text is not None:
                        haystacks.append(formula_text)

                if not haystacks:
                    continue
                if not any(matches_text(text) for text in haystacks):
                    continue

                match_entry: dict[str, Any] = {
                    "address": _format_a1_address(sheet_name=actual_sheet, row=row_num, col=col_num),
                }
                if search_in in {"values", "both"}:
                    match_entry["value"] = _to_jsonable_excel_value(value_obj)
                if search_in in {"formulas", "both"}:
                    match_entry["formula"] = (
                        _truncate_str(formula_text, _get_max_string_chars()) if isinstance(formula_text, str) else None
                    )
                results.append(match_entry)

                if return_mode == "first":
                    truncated = False
                    payload = {
                        "status": "success",
                        "file": str(file_path),
                        "sheet": actual_sheet,
                        "range": f"{sheet_ref}!{range_out}",
                        "query": _to_jsonable_excel_value(query),
                        "match": match_mode,
                        "search_in": search_in,
                        "case_sensitive": case_sensitive,
                        "truncated": False,
                        "scan_truncated": False,
                        "matches": results,
                    }
                    if requested_range_out is not None:
                        payload["requested_range"] = f"{sheet_ref}!{requested_range_out}"
                    metrics = {
                        "status": "success",
                        "file": str(file_path),
                        "sheet": actual_sheet,
                        "scanned_cells": scanned,
                        "requested_cells": requested_cell_count,
                        "candidate_cells": candidate_cell_count,
                        "returned_matches": len(results),
                        "truncated": False,
                        "scan_truncated": False,
                    }
                    text = _json_dumps_compact(payload)
                    if len(text) <= max_response_chars:
                        return payload, metrics

                    matches_out = payload.get("matches")
                    if isinstance(matches_out, list) and matches_out:
                        for match in matches_out:
                            if not isinstance(match, dict):
                                continue
                            match.pop("value", None)
                            match.pop("formula", None)
                        payload["matches"] = matches_out[:1]
                        payload["truncated"] = True
                        metrics["truncated"] = True
                        metrics["returned_matches"] = len(payload["matches"])
                        text = _json_dumps_compact(payload)

                    if len(text) > max_response_chars:
                        payload.pop("query", None)
                        payload.pop("requested_range", None)
                        text = _json_dumps_compact(payload)

                    if len(text) > max_response_chars:
                        addr = None
                        matches_out = payload.get("matches")
                        if isinstance(matches_out, list) and matches_out:
                            first = matches_out[0]
                            if isinstance(first, dict):
                                addr = first.get("address")
                        payload = {
                            "status": "success",
                            "file": str(file_path),
                            "sheet": actual_sheet,
                            "range": f"{sheet_ref}!{range_out}",
                            "truncated": True,
                            "scan_truncated": False,
                            "matches": [{"address": addr}] if isinstance(addr, str) and addr else [],
                        }
                        metrics["truncated"] = True
                        metrics["returned_matches"] = len(payload["matches"])
                    return payload, metrics

                if len(results) >= max_results:
                    truncated = True
                    break
            if truncated or scan_truncated:
                break

        payload = {
            "status": "success",
            "file": str(file_path),
            "sheet": actual_sheet,
            "range": f"{sheet_ref}!{range_out}",
            "query": _to_jsonable_excel_value(query),
            "match": match_mode,
            "search_in": search_in,
            "case_sensitive": case_sensitive,
            "truncated": truncated,
            "scan_truncated": scan_truncated,
            "matches": results,
        }
        if requested_range_out is not None:
            payload["requested_range"] = f"{sheet_ref}!{requested_range_out}"
        metrics = {
            "status": "success",
            "file": str(file_path),
            "sheet": actual_sheet,
            "scanned_cells": scanned,
            "requested_cells": requested_cell_count,
            "candidate_cells": candidate_cell_count,
            "returned_matches": len(results),
            "truncated": truncated,
            "scan_truncated": scan_truncated,
        }
        text = _json_dumps_compact(payload)
        if len(text) <= max_response_chars:
            return payload, metrics

        matches = payload.get("matches")
        if isinstance(matches, list) and matches:
            while len(text) > max_response_chars and len(matches) > 1:
                matches = matches[: max(1, len(matches) // 2)]
                payload["matches"] = matches
                payload["truncated"] = True
                metrics["truncated"] = True
                metrics["returned_matches"] = len(matches)
                text = _json_dumps_compact(payload)

            if len(text) > max_response_chars:
                for match in matches:
                    if not isinstance(match, dict):
                        continue
                    match.pop("value", None)
                    match.pop("formula", None)
                payload["matches"] = matches[:1]
                payload["truncated"] = True
                metrics["truncated"] = True
                metrics["returned_matches"] = len(payload["matches"])
                text = _json_dumps_compact(payload)

        if len(text) > max_response_chars:
            payload.pop("query", None)
            payload.pop("requested_range", None)
            text = _json_dumps_compact(payload)

        if len(text) > max_response_chars:
            addr = None
            matches_out = payload.get("matches")
            if isinstance(matches_out, list) and matches_out:
                first = matches_out[0]
                if isinstance(first, dict):
                    addr = first.get("address")
            payload = {
                "status": "success",
                "file": str(file_path),
                "sheet": actual_sheet,
                "range": f"{sheet_ref}!{range_out}",
                "truncated": True,
                "scan_truncated": scan_truncated,
                "matches": [{"address": addr}] if isinstance(addr, str) and addr else [],
            }
            metrics["truncated"] = True
            metrics["returned_matches"] = len(payload["matches"])
        return payload, metrics
    finally:
        for wb in (wb_formula, wb_values):
            if wb is None:
                continue
            try:
                wb.close()
            except Exception:
                pass


class FindCellsTool(BaseTool):
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
        self.max_response_chars = _get_max_response_chars(config)

    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema:
        return self.tool_schema

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
                return ToolResponse(text="Error: invalid path."), 0.0, {"status": "error", "error": "invalid_path"}
        if relpath.suffix.lower() != ".xlsx":
            return ToolResponse(text="Error: only .xlsx workbooks are supported."), 0.0, {
                "status": "error",
                "error": "invalid_path",
            }

        sheet_name_raw = parameters.get("sheet_name")
        if not isinstance(sheet_name_raw, str) or not sheet_name_raw.strip():
            return ToolResponse(text="Error: sheet_name is required."), 0.0, {
                "status": "error",
                "error": "invalid_sheet_name",
            }
        sheet_name = _normalize_sheet_name(sheet_name_raw)
        if not sheet_name:
            return ToolResponse(text="Error: sheet_name is empty after normalization."), 0.0, {
                "status": "error",
                "error": "invalid_sheet_name",
            }

        query_raw = parameters.get("query")
        if not isinstance(query_raw, str) or not query_raw.strip():
            return ToolResponse(text="Error: query must be a non-empty string."), 0.0, {
                "status": "error",
                "error": "invalid_query",
            }
        query = query_raw.strip()

        try:
            match_mode = _normalize_match_mode(parameters.get("match"))
            search_in = _normalize_search_in(parameters.get("search_in"))
            return_mode = _normalize_return_mode(parameters.get("return"))
        except ValueError as exc:
            return ToolResponse(text=f"Error: {exc}"), 0.0, {"status": "error", "error": "invalid_parameters"}

        if match_mode == "regex":
            max_pat = _get_max_regex_pattern_chars()
            if len(query) > max_pat:
                return ToolResponse(text=f"Error: regex pattern too long (len={len(query)}, max={max_pat})."), 0.0, {
                    "status": "error",
                    "error": "invalid_query",
                }
            regex_error = _validate_regex_pattern(query)
            if regex_error:
                return ToolResponse(text=f"Error: {regex_error}"), 0.0, {
                    "status": "error",
                    "error": "invalid_query",
                }

        case_sensitive_raw = parameters.get("case_sensitive", False)
        if not isinstance(case_sensitive_raw, bool):
            return ToolResponse(text="Error: case_sensitive must be a boolean."), 0.0, {
                "status": "error",
                "error": "invalid_case_sensitive",
            }
        case_sensitive = bool(case_sensitive_raw)

        search_range = parameters.get("range")
        if search_range is not None and (not isinstance(search_range, str) or not search_range.strip()):
            return ToolResponse(text="Error: range must be a non-empty string if provided."), 0.0, {
                "status": "error",
                "error": "invalid_range",
            }

        max_results_raw = parameters.get("max_results", 20)
        try:
            max_results = int(max_results_raw)
        except (TypeError, ValueError):
            max_results = 20
        max_results = min(max(1, max_results), 1000)

        workspace_id = normalize_workspace_id(kwargs.get("workspace_id"))
        if workspace_id is None:
            return ToolResponse(text="Error: workspace_id is missing/invalid."), 0.0, {
                "status": "error",
                "error": "missing_workspace_id",
            }
        file_path = _resolve_workspace_file(workspace_id=workspace_id, relpath=relpath)
        if file_path is None:
            return ToolResponse(text=f"Error: file not found: {relpath}"), 0.0, {
                "status": "error",
                "error": "file_not_found",
            }
        if self.max_file_size_bytes is not None:
            try:
                file_size = file_path.stat().st_size
            except OSError as exc:
                return ToolResponse(text=f"Error: failed to stat file: {exc}"), 0.0, {
                    "status": "error",
                    "error": "stat_failed",
                }
            if file_size > self.max_file_size_bytes:
                max_mb = self.max_file_size_bytes // (1024 * 1024)
                actual_mb = file_size / (1024 * 1024)
                return ToolResponse(text=f"Error: workbook is too large ({actual_mb:.1f}MB > {max_mb}MB)."), 0.0, {
                    "status": "error",
                    "error": "file_too_large",
                }
        zip_error = await asyncio.to_thread(
            _scan_zip_metadata,
            file_path,
            max_members=50_000,
            max_total_uncompressed_bytes=512 * 1024 * 1024,
            max_member_uncompressed_bytes=128 * 1024 * 1024,
            max_ratio=200.0,
        )
        if zip_error:
            return ToolResponse(text=f"Error: workbook rejected by zip safety checks: {zip_error}"), 0.0, {
                "status": "error",
                "error": "zip_limits_exceeded",
            }

        try:
            payload, metrics = await asyncio.to_thread(
                _find_cells_sync,
                file_path=file_path,
                sheet_name=sheet_name,
                query=query,
                search_range=search_range,
                match_mode=match_mode,
                search_in=search_in,
                case_sensitive=case_sensitive,
                return_mode=return_mode,
                max_results=max_results,
                max_response_chars=self.max_response_chars,
            )
        except asyncio.CancelledError:
            raise
        except re.error as exc:
            return ToolResponse(text=f"Error: invalid regex: {exc}"), 0.0, {"status": "error", "error": "invalid_regex"}
        except Exception as exc:
            return ToolResponse(text=f"Error: {exc}"), 0.0, {"status": "error", "error": "find_failed"}

        payload["file"] = str(relpath)
        metrics["file"] = str(relpath)
        return ToolResponse(text=_json_dumps_compact(payload)), 0.0, metrics

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
