from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from verl.utils.paths import get_spreadsheet_rl_data_root, normalize_workspace_id
from verl.utils.rollout_trace import rollout_trace_op

from .base_tool import BaseTool
from .schemas import OpenAIFunctionToolSchema, ToolResponse

_A1_CELL_RE = r"[A-Z]{1,3}[0-9]{1,7}"
_A1_COL_RE = r"[A-Z]{1,3}"
_A1_ROW_RE = r"[0-9]{1,7}"
_A1_RANGE_RE = re.compile(
    rf"^(?:{_A1_CELL_RE}(?::{_A1_CELL_RE})?|{_A1_COL_RE}:{_A1_COL_RE}|{_A1_ROW_RE}:{_A1_ROW_RE})$"
)
_SAFE_SHEET_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

_EXCEL_MAX_ROWS = 1_048_576
_EXCEL_MAX_COLS = 16_384


def _normalize_primary_ext(primary_ext: Any) -> Optional[str]:
    if not isinstance(primary_ext, str):
        return None
    primary_ext = primary_ext.strip()
    if not primary_ext:
        return None
    if len(primary_ext) > 16 or not primary_ext.startswith("."):
        return None
    if not re.fullmatch(r"\.[A-Za-z0-9]+", primary_ext):
        return None
    if primary_ext.lower() != ".xlsx":
        return None
    return primary_ext


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


def _parse_sheet_cell_range(token: str, *, default_sheet_name: str) -> tuple[str, str]:
    token = token.strip(" \t\n\r\f\v\"")
    if not token:
        raise ValueError("empty cell range")

    if "!" in token:
        sheet_part, _, range_part = token.rpartition("!")
        sheet_name = _normalize_sheet_name(sheet_part)
        if not sheet_name:
            raise ValueError(f"empty sheet name: {token!r}")
    else:
        sheet_name = default_sheet_name
        range_part = token

    cell_range = _normalize_cell_range(range_part)
    if not _A1_RANGE_RE.fullmatch(cell_range):
        raise ValueError(f"invalid A1 cell range: {range_part!r}")
    return sheet_name, cell_range


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


def _resolve_safe_file(root: Path, relpath: Path, *, allow_root_symlink: bool = False) -> Optional[Path]:
    if not allow_root_symlink and root.is_symlink():
        return None
    candidate = root / relpath
    try:
        root_resolved = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(root_resolved):
        return None

    if candidate.is_symlink():
        return None

    cursor = root
    for part in relpath.parts:
        cursor = cursor / part
        try:
            if cursor.is_symlink():
                return None
        except OSError:
            return None

    if resolved.is_file():
        return resolved
    return None


def _get_max_cells(config: dict[str, Any]) -> int:
    env_value = os.environ.get("SPREADSHEET_RL_INSPECT_MAX_CELLS", "").strip()
    if env_value:
        try:
            n = int(env_value)
            return min(max(1, n), 100_000)
        except ValueError:
            pass

    try:
        n = int(config.get("max_cells", 400))
        return min(max(1, n), 100_000)
    except Exception:
        return 400


def _get_max_response_chars(config: dict[str, Any]) -> int:
    for raw in (config.get("max_response_chars"), os.environ.get("SPREADSHEET_RL_TOOL_MAX_RESPONSE_CHARS", "").strip()):
        if raw is None:
            continue
        try:
            n = int(raw)
            return min(max(128, n), 8192)
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


def _get_max_cf_rules(config: dict[str, Any]) -> int:
    env_value = os.environ.get("SPREADSHEET_RL_INSPECT_MAX_CF_RULES", "").strip()
    if env_value:
        try:
            n = int(env_value)
            return min(max(0, n), 100_000)
        except ValueError:
            pass

    try:
        n = int(config.get("max_cf_rules", 2000))
        return min(max(0, n), 100_000)
    except Exception:
        return 2000


def _get_max_file_mb(config: dict[str, Any]) -> int:
    env_value = os.environ.get("SPREADSHEET_RL_INSPECT_MAX_FILE_MB", "").strip()
    if env_value:
        try:
            n = int(env_value)
            return min(max(0, n), 10_000)
        except ValueError:
            pass

    try:
        n = int(config.get("max_file_mb", 100))
        return min(max(0, n), 10_000)
    except Exception:
        return 100


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
        return f"failed to scan zip metadata: {exc}"

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


def _sample_linear_indices(total: int, *, head: int, tail: int) -> list[int]:
    if total <= 0:
        return []
    head_n = max(0, head)
    tail_n = max(0, tail)
    if total <= head_n + tail_n:
        return list(range(total))
    indices = list(range(head_n))
    indices.extend(range(total - tail_n, total))
    return indices


def _fill_missing_boundaries(boundaries: tuple[Optional[int], Optional[int], Optional[int], Optional[int]]) -> tuple[int, int, int, int]:
    min_col, min_row, max_col, max_row = boundaries
    if min_col is None:
        min_col = 1
    if max_col is None:
        max_col = _EXCEL_MAX_COLS
    if min_row is None:
        min_row = 1
    if max_row is None:
        max_row = _EXCEL_MAX_ROWS
    return int(min_col), int(min_row), int(max_col), int(max_row)


def _boundaries_intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    a_min_col, a_min_row, a_max_col, a_max_row = a
    b_min_col, b_min_row, b_max_col, b_max_row = b
    if a_max_col < b_min_col or b_max_col < a_min_col:
        return False
    if a_max_row < b_min_row or b_max_row < a_min_row:
        return False
    return True


def _split_sqref(value: Any) -> list[str]:
    if value is None:
        return []
    try:
        if hasattr(value, "ranges"):
            return [str(rng) for rng in list(getattr(value, "ranges"))]
    except Exception:
        pass
    text = str(value).strip()
    if not text:
        return []
    return [part for part in text.split() if part]


def _color_to_json(color: Any) -> Any:
    if color is None:
        return None
    data: dict[str, Any] = {}
    for key in ("type", "rgb", "indexed", "theme", "tint"):
        try:
            value = getattr(color, key)
        except Exception:
            continue
        if key in {"indexed", "theme"} and not isinstance(value, int):
            continue
        if key == "tint" and not isinstance(value, (int, float)):
            continue
        if key in {"type", "rgb"} and not isinstance(value, str):
            continue
        if value is None or value == "":
            continue
        if isinstance(value, float):
            value = value if math.isfinite(value) else str(value)
        data[key] = value
    return data or None


def _side_to_json(side: Any) -> Any:
    if side is None:
        return None
    try:
        style = getattr(side, "style", None)
    except Exception:
        style = None
    try:
        color = getattr(side, "color", None)
    except Exception:
        color = None
    if style is None and color is None:
        return None
    return {"style": style, "color": _color_to_json(color)}


def _style_to_json(cell: Any) -> Any:
    try:
        if not getattr(cell, "has_style", False):
            return None
    except Exception:
        return None

    style: dict[str, Any] = {}
    try:
        style["style"] = getattr(cell, "style", None)
    except Exception:
        pass
    try:
        style_id = getattr(cell, "style_id", None)
        if style_id is not None:
            style["style_id"] = int(style_id)
    except Exception:
        pass

    try:
        font = getattr(cell, "font", None)
    except Exception:
        font = None
    if font is not None:
        font_data: dict[str, Any] = {}
        for key in ("name", "sz", "b", "i", "u", "strike", "outline", "shadow", "vertAlign"):
            try:
                value = getattr(font, key)
            except Exception:
                continue
            if value is None or value is False:
                continue
            font_data[key] = value
        try:
            font_data["color"] = _color_to_json(getattr(font, "color", None))
        except Exception:
            pass
        style["font"] = font_data or None

    try:
        fill = getattr(cell, "fill", None)
    except Exception:
        fill = None
    if fill is not None:
        fill_data: dict[str, Any] = {}
        for key in ("patternType", "fill_type"):
            try:
                value = getattr(fill, key)
            except Exception:
                continue
            if value:
                fill_data["patternType"] = value
                break
        try:
            fill_data["fgColor"] = _color_to_json(getattr(fill, "fgColor", None))
        except Exception:
            pass
        try:
            fill_data["bgColor"] = _color_to_json(getattr(fill, "bgColor", None))
        except Exception:
            pass
        style["fill"] = fill_data or None

    try:
        border = getattr(cell, "border", None)
    except Exception:
        border = None
    if border is not None:
        border_data: dict[str, Any] = {}
        for key, attr in (
            ("left", "left"),
            ("right", "right"),
            ("top", "top"),
            ("bottom", "bottom"),
            ("diagonal", "diagonal"),
        ):
            try:
                side_data = _side_to_json(getattr(border, attr, None))
            except Exception:
                side_data = None
            if side_data is not None:
                border_data[key] = side_data
        style["border"] = border_data or None

    try:
        alignment = getattr(cell, "alignment", None)
    except Exception:
        alignment = None
    if alignment is not None:
        alignment_data: dict[str, Any] = {}
        for key in (
            "horizontal",
            "vertical",
            "wrap_text",
            "shrink_to_fit",
            "text_rotation",
            "indent",
        ):
            try:
                value = getattr(alignment, key)
            except Exception:
                continue
            if value is None or value is False:
                continue
            alignment_data[key] = value
        style["alignment"] = alignment_data or None

    try:
        protection = getattr(cell, "protection", None)
    except Exception:
        protection = None
    if protection is not None:
        protection_data: dict[str, Any] = {}
        for key in ("locked", "hidden"):
            try:
                value = getattr(protection, key)
            except Exception:
                continue
            if value is None or value is False:
                continue
            protection_data[key] = value
        style["protection"] = protection_data or None

    if all(v is None for v in style.values()):
        return None
    return style


def _serialisable_to_json(obj: Any, *, depth: int = 3) -> Any:
    if obj is None:
        return None
    if obj.__class__.__name__ == "Color":
        return _color_to_json(obj) or str(obj)
    if depth <= 0:
        return str(obj)
    if isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (list, tuple, set)):
        items = list(obj)
        return [_serialisable_to_json(item, depth=depth - 1) for item in items[:200]]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(obj.items()):
            if idx >= 200:
                break
            out[str(k)] = _serialisable_to_json(v, depth=depth - 1)
        return out

    attrs = getattr(obj, "__attrs__", None)
    elems = getattr(obj, "__elements__", None)
    keys: list[str] = []
    if isinstance(attrs, (tuple, list)):
        keys.extend([str(item) for item in attrs])
    if isinstance(elems, (tuple, list)):
        keys.extend([str(item) for item in elems])
    if keys:
        out: dict[str, Any] = {"_type": obj.__class__.__name__}
        for key in keys[:200]:
            try:
                value = getattr(obj, key)
            except Exception:
                continue
            out[key] = _serialisable_to_json(value, depth=depth - 1)
        return out

    try:
        raw = vars(obj)
    except Exception:
        raw = None
    if isinstance(raw, dict) and raw:
        out = {"_type": obj.__class__.__name__}
        for idx, (k, v) in enumerate(raw.items()):
            if idx >= 200:
                break
            if str(k).startswith("_"):
                continue
            out[str(k)] = _serialisable_to_json(v, depth=depth - 1)
        if len(out) > 1:
            return out

    return str(obj)


@dataclass(frozen=True)
class _RangeTarget:
    sheet: str
    cell_range: str
    boundaries: tuple[int, int, int, int]


def _load_workbook(path: Path, *, data_only: bool, read_only: bool):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError("openpyxl is required; install openpyxl==3.1.5") from None

    try:
        return load_workbook(
            filename=str(path),
            data_only=data_only,
            read_only=read_only,
            keep_links=False,
        )
    except Exception as exc:
        raise RuntimeError(f"failed to load workbook: {exc}") from None


def _resolve_sheet_name(wb: Any, requested_sheet: str) -> str:
    sheet_map: dict[str, str | None] = {}
    for sheet in wb.sheetnames:
        key = sheet.lower()
        if key not in sheet_map:
            sheet_map[key] = sheet
        elif sheet_map[key] != sheet:
            sheet_map[key] = None

    key = requested_sheet.lower()
    if key not in sheet_map:
        raise ValueError(f"sheet not found: {requested_sheet}")
    actual = sheet_map[key]
    if actual is None:
        raise ValueError(f"ambiguous sheet name: {requested_sheet}")
    return actual


def _build_target(wb: Any, token: str) -> _RangeTarget:
    try:
        from openpyxl.utils.cell import range_boundaries
    except ImportError:
        raise RuntimeError("openpyxl is required; install openpyxl==3.1.5") from None

    if not wb.sheetnames:
        raise RuntimeError("workbook has no sheets")
    default_sheet = wb.sheetnames[0]

    sheet_name, cell_range = _parse_sheet_cell_range(token, default_sheet_name=default_sheet)
    sheet_name = _resolve_sheet_name(wb, sheet_name)

    try:
        boundaries = range_boundaries(cell_range if ":" in cell_range else f"{cell_range}:{cell_range}")
    except Exception as exc:
        raise ValueError(f"failed to parse range boundaries: {exc}") from None

    filled = _fill_missing_boundaries(boundaries)
    if (
        filled[0] < 1
        or filled[1] < 1
        or filled[2] > _EXCEL_MAX_COLS
        or filled[3] > _EXCEL_MAX_ROWS
    ):
        raise ValueError("invalid range boundaries")
    return _RangeTarget(sheet=sheet_name, cell_range=cell_range, boundaries=filled)


def _iter_overlapping_ranges(
    sqref: Any, *, requested_boundaries: tuple[int, int, int, int]
) -> list[str]:
    tokens = _split_sqref(sqref)
    if not tokens:
        return []

    try:
        from openpyxl.utils.cell import range_boundaries
    except ImportError:
        return []

    matches: list[str] = []
    for token in tokens:
        token_norm = _normalize_cell_range(token)
        if not _A1_RANGE_RE.fullmatch(token_norm):
            continue
        try:
            boundaries = range_boundaries(token_norm if ":" in token_norm else f"{token_norm}:{token_norm}")
        except Exception:
            continue
        filled = _fill_missing_boundaries(boundaries)
        if _boundaries_intersect(requested_boundaries, filled):
            matches.append(token_norm)
    return matches


def _extract_auto_filter(ws: Any, *, requested_boundaries: tuple[int, int, int, int]) -> Any:
    auto_filter = getattr(ws, "auto_filter", None)
    if auto_filter is None:
        return None

    ref = getattr(auto_filter, "ref", None)
    applies: Optional[bool] = None
    if isinstance(ref, str) and ref.strip():
        try:
            from openpyxl.utils.cell import range_boundaries
        except ImportError:
            applies = None
        else:
            try:
                filled = _fill_missing_boundaries(range_boundaries(_normalize_cell_range(ref)))
                applies = _boundaries_intersect(requested_boundaries, filled)
            except Exception:
                applies = None

    filter_columns = getattr(auto_filter, "filterColumn", None)
    if not isinstance(filter_columns, list):
        filter_columns = []

    out: dict[str, Any] = {
        "ref": ref,
        "applies_to_requested_range": applies,
        "filter_columns": [_serialisable_to_json(col, depth=3) for col in filter_columns[:50]],
        "sort_state": _serialisable_to_json(getattr(auto_filter, "sortState", None), depth=3),
    }
    return out


def _extract_tables(ws: Any, *, requested_boundaries: tuple[int, int, int, int]) -> list[dict[str, Any]]:
    tables_obj = getattr(ws, "tables", None)
    if tables_obj is None:
        return []

    try:
        if hasattr(tables_obj, "values"):
            tables = list(tables_obj.values())
        else:
            tables = list(tables_obj)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for table in tables[:200]:
        ref = getattr(table, "ref", None)
        if not isinstance(ref, str) or not ref.strip():
            continue
        try:
            from openpyxl.utils.cell import range_boundaries
        except ImportError:
            continue
        try:
            filled = _fill_missing_boundaries(range_boundaries(_normalize_cell_range(ref)))
        except Exception:
            continue
        if not _boundaries_intersect(requested_boundaries, filled):
            continue

        columns: list[str] = []
        try:
            cols = getattr(table, "tableColumns", None)
            if isinstance(cols, list):
                for col in cols[:200]:
                    col_name = getattr(col, "name", None)
                    if isinstance(col_name, str) and col_name:
                        columns.append(col_name)
        except Exception:
            columns = []

        out.append(
            {
                "name": getattr(table, "name", None),
                "displayName": getattr(table, "displayName", None),
                "ref": ref,
                "style": _serialisable_to_json(getattr(table, "tableStyleInfo", None), depth=2),
                "auto_filter": _serialisable_to_json(getattr(table, "autoFilter", None), depth=2),
                "columns": columns or None,
            }
        )
    return out


def _extract_named_ranges(
    wb: Any,
    *,
    ws: Any,
    requested_sheet: str,
    requested_boundaries: tuple[int, int, int, int],
) -> list[dict[str, Any]]:
    wb_defined_names = getattr(wb, "defined_names", None)
    ws_defined_names = getattr(ws, "defined_names", None)
    if wb_defined_names is None and ws_defined_names is None:
        return []

    sources: list[tuple[Any, Optional[str]]] = []
    if wb_defined_names is not None:
        entries: list[Any] = []
        for attr in ("definedName", "defined_names", "values"):
            try:
                maybe = getattr(wb_defined_names, attr)
            except Exception:
                continue
            if isinstance(maybe, list):
                entries = list(maybe)
                break

        if not entries:
            try:
                if hasattr(wb_defined_names, "definedName"):
                    entries = list(getattr(wb_defined_names, "definedName") or [])
            except Exception:
                entries = []

        if not entries:
            try:
                entries = list(wb_defined_names.values())
            except Exception:
                entries = []

        sources.extend([(entry, None) for entry in entries])

    if ws_defined_names is not None:
        try:
            ws_entries = list(ws_defined_names.values())
        except Exception:
            ws_entries = []
        sources.extend([(entry, requested_sheet) for entry in ws_entries])

    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entry, fallback_scope in sources:
        if entry is None:
            continue
        entry_id = id(entry)
        if entry_id in seen:
            continue
        seen.add(entry_id)
        name = getattr(entry, "name", None)
        if not isinstance(name, str) or not name:
            continue

        scope = None
        try:
            local_id = getattr(entry, "localSheetId", None)
        except Exception:
            local_id = None
        if local_id is not None:
            try:
                scope = wb.sheetnames[int(local_id)]
            except Exception:
                scope = None
        elif fallback_scope:
            scope = fallback_scope

        destinations: list[dict[str, str]] = []
        try:
            dest_iter = getattr(entry, "destinations", None)
        except Exception:
            dest_iter = None
        if dest_iter is None:
            continue

        try:
            for sheet_name, rng in list(dest_iter):
                if sheet_name is None:
                    continue
                if not isinstance(sheet_name, str) or not isinstance(rng, str):
                    continue
                if sheet_name.lower() != requested_sheet.lower():
                    continue
                rng_norm = _normalize_cell_range(rng)
                if not _A1_RANGE_RE.fullmatch(rng_norm):
                    continue
                overlaps = _iter_overlapping_ranges(rng_norm, requested_boundaries=requested_boundaries)
                if not overlaps:
                    continue
                destinations.append({"sheet": sheet_name, "range": rng_norm})
        except Exception:
            destinations = []

        if not destinations:
            continue

        out.append(
            {
                "name": name,
                "scope": scope,
                "destinations": destinations,
                "value": getattr(entry, "attr_text", None),
            }
        )
    return out


def _extract_data_validations(ws: Any, *, requested_boundaries: tuple[int, int, int, int]) -> list[dict[str, Any]]:
    data_validations = getattr(ws, "data_validations", None)
    if data_validations is None:
        return []
    rules = getattr(data_validations, "dataValidation", None)
    if not isinstance(rules, list):
        return []

    out: list[dict[str, Any]] = []
    for dv in rules[:200]:
        sqref = getattr(dv, "sqref", None)
        overlaps = _iter_overlapping_ranges(sqref, requested_boundaries=requested_boundaries)
        if not overlaps:
            continue

        out.append(
            {
                "ranges": overlaps,
                "type": getattr(dv, "type", None),
                "operator": getattr(dv, "operator", None),
                "allow_blank": getattr(dv, "allowBlank", None),
                "show_drop_down": getattr(dv, "showDropDown", None),
                "show_error_message": getattr(dv, "showErrorMessage", None),
                "show_input_message": getattr(dv, "showInputMessage", None),
                "formula1": getattr(dv, "formula1", None),
                "formula2": getattr(dv, "formula2", None),
                "prompt_title": getattr(dv, "promptTitle", None),
                "prompt": getattr(dv, "prompt", None),
                "error_title": getattr(dv, "errorTitle", None),
                "error": getattr(dv, "error", None),
                "error_style": getattr(dv, "errorStyle", None),
            }
        )
    return out


def _extract_conditional_formatting(
    ws: Any,
    *,
    requested_boundaries: tuple[int, int, int, int],
    max_rules: int,
) -> list[dict[str, Any]]:
    cf = getattr(ws, "conditional_formatting", None)
    if cf is None:
        return []

    out: list[dict[str, Any]] = []
    if max_rules <= 0:
        return out

    for cf_item in list(cf)[:200]:
        sqref = getattr(cf_item, "sqref", None)
        if sqref is None:
            continue
        overlaps = _iter_overlapping_ranges(str(sqref), requested_boundaries=requested_boundaries)
        if not overlaps:
            continue

        rules = getattr(cf_item, "rules", None)
        if not isinstance(rules, list):
            continue
        for rule in rules[:200]:
            data: dict[str, Any] = {
                "ranges": overlaps,
                "type": getattr(rule, "type", None),
                "operator": getattr(rule, "operator", None),
                "priority": getattr(rule, "priority", None),
                "stop_if_true": getattr(rule, "stopIfTrue", None),
                "text": getattr(rule, "text", None),
                "time_period": getattr(rule, "timePeriod", None),
                "formula": getattr(rule, "formula", None),
                "dxf": _serialisable_to_json(getattr(rule, "dxf", None), depth=2),
            }
            out.append(data)
            if len(out) >= max_rules:
                return out

    return out


def _extract_merged_ranges(ws: Any, *, requested_boundaries: tuple[int, int, int, int]) -> list[str]:
    merged_cells = getattr(ws, "merged_cells", None)
    if merged_cells is None:
        return []

    ranges = getattr(merged_cells, "ranges", None)
    if ranges is None:
        return []

    out: list[str] = []
    try:
        from openpyxl.utils.cell import range_boundaries
    except ImportError:
        return []

    for rng in list(ranges)[:500]:
        coord = getattr(rng, "coord", None)
        if not isinstance(coord, str) or not coord:
            coord = str(rng)
        coord_norm = _normalize_cell_range(coord)
        if not _A1_RANGE_RE.fullmatch(coord_norm):
            continue
        try:
            filled = _fill_missing_boundaries(range_boundaries(coord_norm))
        except Exception:
            continue
        if _boundaries_intersect(requested_boundaries, filled):
            out.append(coord_norm)
    return out


def _extract_hidden_dims(ws: Any, *, min_row: int, max_row: int, min_col: int, max_col: int) -> dict[str, Any]:
    try:
        from openpyxl.utils import column_index_from_string, get_column_letter
    except ImportError:
        return {"rows": [], "columns": []}

    hidden_rows: list[int] = []
    row_dimensions = getattr(ws, "row_dimensions", None)
    if row_dimensions is not None:
        for row in range(min_row, max_row + 1):
            dim = None
            try:
                dim = row_dimensions.get(row)
            except Exception:
                dim = None
            if dim is None:
                continue
            if getattr(dim, "hidden", None) is True:
                hidden_rows.append(row)

    hidden_col_indices: set[int] = set()
    col_dimensions = getattr(ws, "column_dimensions", None)
    if col_dimensions is not None:
        try:
            items = col_dimensions.items()
        except Exception:
            items = []
        for letter, dim in items:
            if getattr(dim, "hidden", None) is not True:
                continue

            span_min = getattr(dim, "min", None)
            span_max = getattr(dim, "max", None)
            if isinstance(span_min, int) and isinstance(span_max, int) and span_min <= span_max:
                idx_min = span_min
                idx_max = span_max
            elif not isinstance(letter, str) or not letter:
                continue
            elif ":" in letter:
                left, _, right = letter.partition(":")
                try:
                    idx_min = column_index_from_string(left)
                    idx_max = column_index_from_string(right)
                except Exception:
                    continue
                if idx_max < idx_min:
                    idx_min, idx_max = idx_max, idx_min
            else:
                try:
                    idx_min = idx_max = column_index_from_string(letter)
                except Exception:
                    continue

            if idx_max < min_col or idx_min > max_col:
                continue
            start = max(idx_min, min_col)
            end = min(idx_max, max_col)
            for idx in range(start, end + 1):
                hidden_col_indices.add(idx)

    hidden_cols = [get_column_letter(idx) for idx in sorted(hidden_col_indices)]
    return {"rows": hidden_rows, "columns": hidden_cols}


def _inspect_range_sync(
    *,
    file_path: Path,
    range_token: str,
    max_cells: int,
    max_cf_rules: int,
    include_details: bool,
    max_response_chars: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    wb = _load_workbook(file_path, data_only=False, read_only=not include_details)
    try:
        target = _build_target(wb, range_token)
        sheet = target.sheet
        cell_range = target.cell_range
        min_col, min_row, max_col, max_row = target.boundaries
        cell_count = (max_col - min_col + 1) * (max_row - min_row + 1)
        if cell_count <= 0:
            raise ValueError("invalid range size")

        ws = wb[sheet]

        rows = max_row - min_row + 1
        cols = max_col - min_col + 1

        truncated = cell_count > max_cells
        wb_values = None
        ws_values = None
        try:
            cells: list[dict[str, Any]] = []
            if not truncated:
                for r_idx, row in enumerate(
                    ws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col),
                    start=0,
                ):
                    row_num = min_row + r_idx
                    for c_idx, cell in enumerate(row, start=0):
                        col_num = min_col + c_idx
                        is_formula = getattr(cell, "data_type", None) == "f"
                        formula = None
                        if is_formula:
                            raw = getattr(cell, "value", None)
                            if isinstance(raw, str) and raw:
                                formula = raw if raw.startswith("=") else f"={raw}"
                                formula = _truncate_str(formula, _get_max_string_chars())
                        if wb_values is None and is_formula:
                            wb_values = _load_workbook(file_path, data_only=True, read_only=True)
                            ws_values = wb_values[sheet]

                        if is_formula and ws_values is not None:
                            try:
                                val = ws_values.cell(row=row_num, column=col_num).value
                            except Exception:
                                val = None
                        else:
                            val = getattr(cell, "value", None)

                        entry: dict[str, Any] = {
                            "address": _format_a1_address(sheet_name=sheet, row=row_num, col=col_num),
                            "value": _to_jsonable_excel_value(val),
                            "formula": formula,
                        }
                        if include_details:
                            try:
                                entry["number_format"] = getattr(cell, "number_format", None)
                            except Exception:
                                entry["number_format"] = None
                            entry["style"] = _style_to_json(cell)
                        cells.append(entry)
            else:
                sample_indices = _sample_linear_indices(cell_count, head=5, tail=5)
                for idx in sample_indices:
                    r_off = idx // cols
                    c_off = idx - r_off * cols
                    row_num = min_row + r_off
                    col_num = min_col + c_off
                    try:
                        cell = ws.cell(row=row_num, column=col_num)
                    except Exception:
                        cell = None
                    is_formula = getattr(cell, "data_type", None) == "f" if cell is not None else False
                    formula = None
                    if is_formula:
                        raw = getattr(cell, "value", None)
                        if isinstance(raw, str) and raw:
                            formula = raw if raw.startswith("=") else f"={raw}"
                            formula = _truncate_str(formula, _get_max_string_chars())
                    if wb_values is None and is_formula:
                        wb_values = _load_workbook(file_path, data_only=True, read_only=True)
                        ws_values = wb_values[sheet]

                    if is_formula and ws_values is not None:
                        try:
                            val = ws_values.cell(row=row_num, column=col_num).value
                        except Exception:
                            val = None
                    else:
                        val = getattr(cell, "value", None) if cell is not None else None

                    entry = {
                        "address": _format_a1_address(sheet_name=sheet, row=row_num, col=col_num),
                        "value": _to_jsonable_excel_value(val),
                        "formula": formula,
                    }
                    if include_details and cell is not None:
                        try:
                            entry["number_format"] = getattr(cell, "number_format", None)
                        except Exception:
                            entry["number_format"] = None
                        entry["style"] = _style_to_json(cell)
                    cells.append(entry)

            payload: dict[str, Any] = {
                "status": "success",
                "file": str(file_path),
                "sheet": sheet,
                "range": f"{_quote_sheet_name_for_a1(sheet)}!{cell_range}",
                "shape": {
                    "rows": rows,
                    "cols": cols,
                    "cells": cell_count,
                    "min_col": min_col,
                    "min_row": min_row,
                    "max_col": max_col,
                    "max_row": max_row,
                },
                "truncated": truncated,
                "cells": cells,
            }

            if include_details and not truncated:
                merged_ranges = _extract_merged_ranges(ws, requested_boundaries=target.boundaries)
                hidden = _extract_hidden_dims(ws, min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col)
                tables = _extract_tables(ws, requested_boundaries=target.boundaries)
                named_ranges = _extract_named_ranges(wb, ws=ws, requested_sheet=sheet, requested_boundaries=target.boundaries)
                auto_filter = _extract_auto_filter(ws, requested_boundaries=target.boundaries)
                data_validations = _extract_data_validations(ws, requested_boundaries=target.boundaries)
                conditional_formatting = _extract_conditional_formatting(
                    ws,
                    requested_boundaries=target.boundaries,
                    max_rules=max_cf_rules,
                )
                payload.update(
                    {
                        "merged_ranges": merged_ranges,
                        "hidden": hidden,
                        "tables": tables,
                        "named_ranges": named_ranges,
                        "auto_filter": auto_filter,
                        "data_validations": data_validations,
                        "conditional_formatting": conditional_formatting,
                    }
                )

            metrics = {
                "status": "success",
                "file": str(file_path),
                "sheet": sheet,
                "requested_cells": cell_count,
                "returned_cells": len(cells),
                "truncated": truncated,
                "include_details": include_details,
            }
            text = _json_dumps_compact(payload)
            if len(text) <= max_response_chars:
                return payload, metrics

            metadata_keys = (
                "merged_ranges",
                "hidden",
                "tables",
                "named_ranges",
                "auto_filter",
                "data_validations",
                "conditional_formatting",
            )
            removed_any = False
            for key in metadata_keys:
                if key in payload:
                    removed_any = True
                payload.pop(key, None)
            if removed_any:
                payload["truncated"] = True
                metrics["truncated"] = True

            text = _json_dumps_compact(payload)
            if len(text) <= max_response_chars:
                return payload, metrics

            original_cells = payload.get("cells")
            if not isinstance(original_cells, list) or not original_cells:
                return payload, metrics

            for head, tail in ((5, 5), (3, 3), (2, 2), (1, 1)):
                if len(original_cells) <= head + tail:
                    candidate_cells = original_cells
                else:
                    candidate_cells = original_cells[:head] + original_cells[-tail:]
                payload["cells"] = candidate_cells
                payload["truncated"] = True
                metrics["truncated"] = True
                metrics["returned_cells"] = len(candidate_cells)
                text = _json_dumps_compact(payload)
                if len(text) <= max_response_chars:
                    return payload, metrics

            trimmed_cells = payload.get("cells")
            if isinstance(trimmed_cells, list):
                for cell in trimmed_cells:
                    if not isinstance(cell, dict):
                        continue
                    cell.pop("style", None)
                    cell.pop("number_format", None)
            text = _json_dumps_compact(payload)
            if len(text) <= max_response_chars:
                return payload, metrics

            payload["cells"] = payload["cells"][:1]
            metrics["returned_cells"] = len(payload["cells"])
            return payload, metrics
        finally:
            if wb_values is not None:
                try:
                    wb_values.close()
                except Exception:
                    pass
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _resolve_workbook_path(
    *,
    workspace_id: Optional[str],
    thread_dir: Optional[str],
    primary_ext: Optional[str],
    relpath: Optional[Path],
) -> tuple[Optional[Path], list[Path]]:
    candidates: list[Path] = []
    if relpath is not None:
        rel_candidates = [relpath]
        if relpath.parts and relpath.parts[0] == "data" and len(relpath.parts) > 1:
            rel_candidates.append(Path(*relpath.parts[1:]))
        for cand in rel_candidates:
            candidates.append(cand)

    ext = _normalize_primary_ext(primary_ext)
    if relpath is None:
        if thread_dir and ext:
            candidates.append(Path(thread_dir) / f"output{ext}")
        if thread_dir:
            candidates.append(Path(thread_dir) / "output.xlsx")
        if ext:
            candidates.append(Path(f"output{ext}"))
        candidates.append(Path("output.xlsx"))

    seen: set[Path] = set()
    unique: list[Path] = []
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)

    attempted: list[Path] = []
    if not workspace_id:
        return None, attempted

    workspace_base = get_spreadsheet_rl_data_root()
    workspaces_base = workspace_base / "_workspaces"
    try:
        if workspace_base.is_symlink():
            return None, attempted
        workspaces_base_resolved = workspaces_base.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, attempted

    root = workspaces_base / workspace_id
    try:
        if root.is_symlink():
            return None, attempted
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, attempted
    if not root_resolved.is_relative_to(workspaces_base_resolved):
        return None, attempted
    for cand in unique:
        attempted.append(root / cand)
        resolved = _resolve_safe_file(root, cand)
        if resolved is not None:
            return resolved, attempted
    return None, attempted


class InspectRangeTool(BaseTool):
    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict[str, Any]] = {}
        self.max_cells = _get_max_cells(config)
        self.max_cf_rules = _get_max_cf_rules(config)
        self.max_file_mb = _get_max_file_mb(config)
        self.max_response_chars = _get_max_response_chars(config)

    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema:
        return self.tool_schema

    async def create(self, instance_id: Optional[str] = None, **kwargs) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {}
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs) -> tuple[ToolResponse, float, dict]:
        range_token_raw = parameters.get("range")
        if not isinstance(range_token_raw, str) or not range_token_raw.strip():
            return ToolResponse(text="Error: missing/invalid 'range' (A1 notation)."), 0.0, {
                "status": "error",
                "error": "invalid_range",
            }
        range_token = range_token_raw.strip()
        include_details_raw = parameters.get("include_details", False)
        if not isinstance(include_details_raw, bool):
            return ToolResponse(text="Error: include_details must be a boolean."), 0.0, {
                "status": "error",
                "error": "invalid_include_details",
            }
        include_details = bool(include_details_raw)
        try:
            from openpyxl.utils.cell import range_boundaries
        except ImportError:
            return ToolResponse(text="Error: openpyxl is required; install openpyxl==3.1.5."), 0.0, {
                "status": "error",
                "error": "missing_dependency",
            }

        try:
            _, cell_range = _parse_sheet_cell_range(range_token, default_sheet_name="Sheet1")
            boundaries = range_boundaries(cell_range if ":" in cell_range else f"{cell_range}:{cell_range}")
            filled = _fill_missing_boundaries(boundaries)
            min_col, min_row, max_col, max_row = filled
            if (
                min_col < 1
                or min_row < 1
                or max_col > _EXCEL_MAX_COLS
                or max_row > _EXCEL_MAX_ROWS
            ):
                raise ValueError("range out of bounds")
            cell_count = (max_col - min_col + 1) * (max_row - min_row + 1)
        except Exception as exc:
            return ToolResponse(text=f"Error: {exc}"), 0.0, {"status": "error", "error": "invalid_range"}

        if cell_count <= 0:
            return ToolResponse(text="Error: invalid range size."), 0.0, {"status": "error", "error": "invalid_range"}

        workspace_id = normalize_workspace_id(kwargs.get("workspace_id"))
        if workspace_id is None:
            return ToolResponse(text="Error: workspace_id is missing/invalid."), 0.0, {
                "status": "error",
                "error": "missing_workspace_id",
            }

        path_raw = parameters.get("path")
        if path_raw is None or (isinstance(path_raw, str) and not path_raw.strip()):
            relpath = Path("data.xlsx")
        else:
            relpath = _sanitize_relpath(path_raw)
            if relpath is None:
                return ToolResponse(text="Error: invalid 'path'."), 0.0, {"status": "error", "error": "invalid_path"}
        if relpath.suffix.lower() != ".xlsx":
            return ToolResponse(text="Error: only .xlsx workbooks are supported."), 0.0, {
                "status": "error",
                "error": "invalid_path",
            }

        file_path, attempted = _resolve_workbook_path(
            workspace_id=workspace_id,
            thread_dir=None,
            primary_ext=None,
            relpath=relpath,
        )
        if file_path is None:
            attempted_display: list[str] = []
            root = get_spreadsheet_rl_data_root() / "_workspaces" / workspace_id
            for p in attempted:
                try:
                    attempted_display.append(str(p.relative_to(root)))
                except Exception:
                    attempted_display.append(str(p))
            attempted_text = "\n".join(attempted_display[:12])
            return ToolResponse(
                text=(
                    "Error: workbook not found.\n"
                    f"Tried:\n{attempted_text}\n"
                    "Save the workbook as data.xlsx in the workspace root, or pass 'path' explicitly."
                )
            ), 0.0, {"status": "error", "error": "workbook_not_found"}

        try:
            if self.max_file_mb > 0:
                try:
                    file_size = file_path.stat().st_size
                except OSError:
                    file_size = None
                if file_size is not None and file_size > self.max_file_mb * 1024 * 1024:
                    return ToolResponse(
                        text=(
                            "Error: workbook too large to inspect.\n"
                            f"file: {relpath}\n"
                            f"size_bytes: {file_size}\n"
                            f"max_file_mb: {self.max_file_mb}"
                        )
                    ), 0.0, {"status": "error", "error": "file_too_large", "size_bytes": int(file_size)}

            zip_error = await asyncio.to_thread(
                _scan_zip_metadata,
                file_path,
                max_members=50_000,
                max_total_uncompressed_bytes=512 * 1024 * 1024,
                max_member_uncompressed_bytes=128 * 1024 * 1024,
                max_ratio=200.0,
            )
            if zip_error:
                return ToolResponse(
                    text=(
                        "Error: workbook rejected by zip safety checks.\n"
                        f"file: {relpath}\n"
                        f"detail: {zip_error}"
                    )
                ), 0.0, {"status": "error", "error": "zip_limits_exceeded"}

            payload, metrics = await asyncio.to_thread(
                _inspect_range_sync,
                file_path=file_path,
                range_token=range_token,
                max_cells=self.max_cells,
                max_cf_rules=self.max_cf_rules,
                include_details=include_details,
                max_response_chars=self.max_response_chars,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResponse(text=f"Error: {exc}"), 0.0, {"status": "error", "error": "inspect_failed"}

        payload["file"] = str(relpath)
        metrics["file"] = str(relpath)
        return ToolResponse(text=_json_dumps_compact(payload)), 0.0, metrics

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
