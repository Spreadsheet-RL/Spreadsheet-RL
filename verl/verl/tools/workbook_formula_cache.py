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

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from posixpath import normpath
from typing import Optional
from zipfile import ZipFile


@dataclass(frozen=True)
class _CachedFormulaValue:
    cell_type: Optional[str]
    value: str


def _normalize_relationship_target(target: str) -> Optional[str]:
    token = str(target or "").replace("\\", "/").strip()
    if not token:
        return None
    if token.startswith("/"):
        normalized = normpath(token.lstrip("/"))
    elif token.startswith("xl/"):
        normalized = normpath(token)
    else:
        normalized = normpath(f"xl/{token}")
    if normalized.startswith("../") or normalized == "..":
        return None
    return normalized


def _worksheet_part_paths_by_title(path: Path) -> dict[str, str]:
    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    try:
        with ZipFile(path, "r") as zin:
            workbook_root = ET.fromstring(zin.read("xl/workbook.xml"))
            rels_root = ET.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
    except Exception:
        return {}

    rel_targets: dict[str, str] = {}
    for rel in rels_root.iter(f"{package_rel_ns}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        rel_id = rel.get("Id")
        target = rel.get("Target")
        if not rel_id or not target:
            continue
        normalized = _normalize_relationship_target(target)
        if normalized:
            rel_targets[rel_id] = normalized

    parts_by_title: dict[str, str] = {}
    for sheet in workbook_root.iter(f"{main_ns}sheet"):
        title = sheet.get("name")
        rel_id = sheet.get(f"{rel_ns}id")
        if title and rel_id and rel_id in rel_targets:
            parts_by_title[title] = rel_targets[rel_id]
    return parts_by_title


def _collect_formula_cached_values_from_xlsx(path: Path) -> dict[str, dict[str, _CachedFormulaValue]]:
    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    cached: dict[str, dict[str, _CachedFormulaValue]] = {}
    parts_by_title = _worksheet_part_paths_by_title(path)
    if not parts_by_title:
        return {}
    try:
        with ZipFile(path, "r") as zin:
            names = set(zin.namelist())
            for sheet_title, part_path in parts_by_title.items():
                if part_path not in names:
                    continue
                try:
                    root = ET.fromstring(zin.read(part_path))
                except Exception:
                    continue
                sheet_cached: dict[str, _CachedFormulaValue] = {}
                for cell in root.iter(f"{main_ns}c"):
                    ref = cell.get("r")
                    if not ref or cell.find(f"{main_ns}f") is None:
                        continue
                    value = cell.find(f"{main_ns}v")
                    if value is not None and value.text is not None:
                        sheet_cached[ref] = _CachedFormulaValue(cell_type=cell.get("t"), value=value.text)
                if sheet_cached:
                    cached[sheet_title] = sheet_cached
    except Exception:
        return {}
    return cached


def _restore_formula_cached_values_in_xlsx(
    path: Path,
    cached: dict[str, dict[str, _CachedFormulaValue]],
    *,
    title_map: Optional[dict[str, str]] = None,
) -> None:
    if not cached:
        return

    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    mapped_cached: dict[str, dict[str, _CachedFormulaValue]] = {}
    title_map = title_map or {}
    for old_title, sheet_cached in cached.items():
        mapped_cached[old_title] = sheet_cached
        if old_title in title_map:
            mapped_cached[title_map[old_title]] = sheet_cached

    parts_by_title = _worksheet_part_paths_by_title(path)
    cached_by_part = {part: mapped_cached[title] for title, part in parts_by_title.items() if title in mapped_cached}
    if not cached_by_part:
        return

    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.cache.xlsx")
    modified = False
    try:
        with ZipFile(path, "r") as zin, ZipFile(tmp_path, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                sheet_cached = cached_by_part.get(item.filename)
                if sheet_cached:
                    root = ET.fromstring(data)
                    sheet_modified = False
                    for cell in root.iter(f"{main_ns}c"):
                        ref = cell.get("r")
                        if ref not in sheet_cached or cell.find(f"{main_ns}f") is None:
                            continue
                        cached_value = sheet_cached[ref]
                        value = cell.find(f"{main_ns}v")
                        if value is None:
                            value = ET.SubElement(cell, f"{main_ns}v")
                        if value.text != cached_value.value:
                            value.text = cached_value.value
                            sheet_modified = True
                        if cached_value.cell_type is not None and cell.get("t") != cached_value.cell_type:
                            cell.set("t", cached_value.cell_type)
                            sheet_modified = True
                    if sheet_modified:
                        # openpyxl rewrites worksheet XML with the default spreadsheet namespace and r namespace only.
                        ET.register_namespace("", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
                        ET.register_namespace(
                            "r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                        )
                        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                        modified = True
                zout.writestr(item, data)
        if modified:
            tmp_path.replace(path)
        else:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"failed to preserve formula cached values: {exc}") from None
