from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional

from verl.utils.paths import normalize_workspace_id
from verl.utils.rollout_trace import rollout_trace_op

from .base_tool import BaseTool
from .find_cells import (
    _error_tool_response,
    _get_max_response_chars,
    _json_dumps_compact,
    _resolve_workspace_file,
    _sanitize_relpath,
    _scan_zip_metadata,
)
from .schemas import OpenAIFunctionToolSchema, ToolResponse


def _worksheet_dimensions(ws) -> str:
    try:
        dimensions = ws.calculate_dimension()
    except TypeError:
        try:
            dimensions = ws.calculate_dimension(force=True)
        except Exception:
            dimensions = None
    except Exception:
        dimensions = None
    if isinstance(dimensions, str) and dimensions.strip():
        return dimensions.strip()
    try:
        dimensions = getattr(ws, "dimensions", None)
    except Exception:
        dimensions = None
    if isinstance(dimensions, str) and dimensions.strip():
        return dimensions.strip()
    return "A1:A1"


def _list_sheets_sync(file_path: Path) -> dict[str, Any]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise RuntimeError("openpyxl is required; install openpyxl>=3.1.5") from None

    wb = None
    try:
        wb = load_workbook(filename=str(file_path), data_only=False, read_only=True, keep_links=False)
        active_sheet = None
        active_index = None
        try:
            active = wb.active
            active_sheet = getattr(active, "title", None)
            active_index = wb.worksheets.index(active)
        except Exception:
            active_sheet = None
            active_index = None

        sheets = []
        for index, ws in enumerate(getattr(wb, "worksheets", None) or []):
            state = getattr(ws, "sheet_state", "visible") or "visible"
            sheets.append({
                "name": getattr(ws, "title", ""),
                "index": index,
                "dimensions": _worksheet_dimensions(ws),
                "max_row": int(getattr(ws, "max_row", 1) or 1),
                "max_col": int(getattr(ws, "max_column", 1) or 1),
                "state": state,
                "hidden": state != "visible",
                "active": index == active_index,
            })

        sheet_count = len(sheets)
        return {
            "status": "success",
            "file": str(file_path),
            "active_sheet": active_sheet,
            "active_index": active_index,
            "sheet_count": sheet_count,
            "returned_sheets": sheet_count,
            "truncated": False,
            "sheets": sheets,
        }
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass


def _mark_list_sheets_truncated(payload: dict[str, Any]) -> None:
    payload["truncated"] = True
    payload["response_truncated"] = True
    payload["truncation_reasons"] = ["response_budget"]


def _fit_list_sheets_payload(payload: dict[str, Any], max_response_chars: int) -> str:
    text = _json_dumps_compact(payload)
    if len(text) <= max_response_chars:
        return text

    sheets = list(payload.get("sheets", []))
    _mark_list_sheets_truncated(payload)

    best_text: Optional[str] = None
    best_sheets: list[dict[str, Any]] = []
    low = 0
    high = len(sheets)
    while low <= high:
        mid = (low + high) // 2
        payload["sheets"] = sheets[:mid]
        payload["returned_sheets"] = mid
        text = _json_dumps_compact(payload)
        if len(text) <= max_response_chars:
            best_text = text
            best_sheets = sheets[:mid]
            low = mid + 1
        else:
            high = mid - 1

    if best_text is not None:
        payload["sheets"] = best_sheets
        payload["returned_sheets"] = len(best_sheets)
        return best_text

    minimal_payload: dict[str, Any] = {
        "status": "success",
        "file": payload.get("file"),
        "sheet_count": payload.get("sheet_count", len(sheets)),
        "returned_sheets": 0,
        "truncated": True,
        "response_truncated": True,
        "truncation_reasons": ["response_budget"],
    }
    text = _json_dumps_compact(minimal_payload)
    if len(text) > max_response_chars:
        minimal_payload.pop("file", None)
        text = _json_dumps_compact(minimal_payload)
    if len(text) > max_response_chars:
        minimal_payload = {"status": "success", "truncated": True}
        text = _json_dumps_compact(minimal_payload)
    payload.clear()
    payload.update(minimal_payload)
    return text


class ListSheetsTool(BaseTool):
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
                return _error_tool_response("invalid_path", "invalid path.")
        if relpath.suffix.lower() != ".xlsx":
            return _error_tool_response("invalid_path", "only .xlsx workbooks are supported.")

        workspace_id = normalize_workspace_id(kwargs.get("workspace_id"))
        if workspace_id is None:
            return _error_tool_response("missing_workspace_id", "workspace_id is missing/invalid.")
        file_path = _resolve_workspace_file(workspace_id=workspace_id, relpath=relpath)
        if file_path is None:
            return _error_tool_response("file_not_found", f"file not found: {relpath}")

        if self.max_file_size_bytes is not None:
            try:
                file_size = file_path.stat().st_size
            except OSError as exc:
                return _error_tool_response("stat_failed", f"failed to stat file: {exc}")
            if file_size > self.max_file_size_bytes:
                max_mb = self.max_file_size_bytes // (1024 * 1024)
                actual_mb = file_size / (1024 * 1024)
                return _error_tool_response(
                    "file_too_large",
                    f"workbook is too large ({actual_mb:.1f}MB > {max_mb}MB).",
                )

        zip_error = await asyncio.to_thread(
            _scan_zip_metadata,
            file_path,
            max_members=50_000,
            max_total_uncompressed_bytes=512 * 1024 * 1024,
            max_member_uncompressed_bytes=128 * 1024 * 1024,
            max_ratio=200.0,
        )
        if zip_error:
            return _error_tool_response("zip_limits_exceeded", f"workbook rejected by zip safety checks: {zip_error}")

        try:
            payload = await asyncio.to_thread(_list_sheets_sync, file_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _error_tool_response("list_sheets_failed", str(exc))

        payload["file"] = str(relpath)
        payload["truncated"] = False
        sheet_count = payload.get("sheet_count", len(payload.get("sheets", [])))
        response_text = _fit_list_sheets_payload(payload, self.max_response_chars)
        metrics = {
            "status": "success",
            "file": str(relpath),
            "sheet_count": sheet_count,
            "returned_sheets": payload.get("returned_sheets", len(payload.get("sheets", []))),
            "truncated": bool(payload.get("truncated")),
        }
        return ToolResponse(text=response_text), 0.0, metrics

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
