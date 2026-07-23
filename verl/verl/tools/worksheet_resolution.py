# Copyright 2026 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from typing import Any


class WorksheetResolutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, matches: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.matches = matches


def _format_available_sheets(sheetnames: list[str]) -> str:
    shown = ", ".join(repr(name) for name in sheetnames[:10])
    if len(sheetnames) > 10:
        return f"{shown} (+{len(sheetnames) - 10} more)"
    return shown or "(none)"


def resolve_worksheet(wb: Any, requested_name: str):
    worksheets = list(getattr(wb, "worksheets", None) or [])
    worksheet_by_name = {str(getattr(ws, "title", "")): ws for ws in worksheets}
    sheetnames = [str(name) for name in (getattr(wb, "sheetnames", None) or [])]
    normalizers = (lambda value: value, str.casefold, lambda value: value.strip().casefold())
    for normalize in normalizers:
        requested_key = normalize(requested_name)
        matches = tuple(name for name in sheetnames if normalize(name) == requested_key)
        if not matches:
            continue
        if len(matches) > 1:
            candidates = ", ".join(repr(name) for name in matches[:5])
            raise WorksheetResolutionError(
                "ambiguous_sheet_name",
                f"ambiguous sheet name: {requested_name!r} matches {candidates}",
                matches=matches,
            )
        worksheet = worksheet_by_name.get(matches[0])
        if worksheet is None:
            raise WorksheetResolutionError(
                "not_a_worksheet",
                f"sheet is not a worksheet: {requested_name!r}",
                matches=matches,
            )
        return worksheet
    raise WorksheetResolutionError(
        "sheet_not_found",
        f"sheet not found: {requested_name!r}; available sheets: {_format_available_sheets(sheetnames)}",
    )
