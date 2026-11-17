"""Inspect public oracle dependency hints and exported XML without changing it.

Use the immutable, already-built audit runtime image. Results are private because
they contain oracle formulas and cached values, never policy observations.
"""

import hashlib
import json
import os
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import modal


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCUMENT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
VOLATILE = re.compile(r"(?i)(?<![A-Z0-9_.])(?:_xlfn\.)?(TODAY|NOW|RAND|RANDBETWEEN|RANDARRAY)\s*\(")


def inspect_xml(path):
    result = {
        "volatile_formula_hints": [],
        "volatile_counts": Counter(),
        "invalid_cf_operators": [],
        "invalid_filter_operators": [],
        "operator_metadata": [],
        "cells": [],
    }
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in relations.findall(f"{{{REL}}}Relationship")}
        calc = workbook.find("m:calcPr", NS)
        result["calculation_settings"] = dict(calc.attrib) if calc is not None else {}
        result["external_link_parts"] = [
            name for name in archive.namelist() if name.startswith("xl/externalLinks/") and name.endswith(".xml")
        ]
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            target = targets[sheet.get(f"{{{DOCUMENT_REL}}}id")]
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            root = ET.fromstring(archive.read(member))
            for cell in root.findall("m:sheetData/m:row/m:c", NS):
                formula, cached = cell.find("m:f", NS), cell.find("m:v", NS)
                text = formula.text or "" if formula is not None else ""
                hints = VOLATILE.findall(text)
                result["volatile_counts"].update(name.upper() for name in hints)
                if hints and len(result["volatile_formula_hints"]) < 20:
                    result["volatile_formula_hints"].append(
                        {"sheet": sheet.get("name"), "cell": cell.get("r"), "formula": text[:300]}
                    )
                if cell.get("r") in {"A2", "B2", "A5"} and len(result["cells"]) < 30:
                    result["cells"].append(
                        {
                            "sheet": sheet.get("name"),
                            "cell": cell.get("r"),
                            "formula": text[:300],
                            "cached": cached.text if cached is not None else None,
                            "type": cell.get("t", "n"),
                        }
                    )
            comparisons = {"lessThanOrEqual", "equal", "lessThan", "greaterThanOrEqual", "notEqual", "greaterThan"}
            for node in root.iter():
                if "operator" in node.attrib:
                    metadata = {
                        "sheet": sheet.get("name"),
                        "element": node.tag.rsplit("}", 1)[-1],
                        "operator": node.get("operator"),
                        "value": node.get("val"),
                    }
                    if len(result["operator_metadata"]) < 40:
                        result["operator_metadata"].append(metadata)
                    if metadata["element"] == "customFilter" and metadata["operator"] not in comparisons:
                        result["invalid_filter_operators"].append(metadata)
            for formatting in root.findall("m:conditionalFormatting", NS):
                for rule in formatting.findall("m:cfRule", NS):
                    operator = rule.get("operator")
                    if operator is not None and operator not in comparisons | {
                        "between",
                        "notBetween",
                        "containsText",
                        "notContains",
                        "beginsWith",
                        "endsWith",
                    }:
                        result["invalid_cf_operators"].append(
                            {
                                "sheet": sheet.get("name"),
                                "range": formatting.get("sqref"),
                                "type": rule.get("type"),
                                "operator": operator,
                            }
                        )
        result["volatile_counts"] = dict(result["volatile_counts"])
    return result


app = modal.App("spreadsheet-oracle-dependency-inspection")
volume = modal.Volume.from_name("spreadsheet-gym-runtime-builds", create_if_missing=False)
# Runtime-only image produced from runtime.lock.json, before source overlay.
image = modal.Image.from_id(os.environ.get("SPREADSHEET_AUDIT_BASE_IMAGE", "im-FjC94w7UFJ9cMoCfxuy0cy"))


@app.function(image=image, volumes={"/artifacts": volume}, cpu=1, memory=4096, timeout=300, block_network=True)
def inspect_tasks(run_id, task_ids, source_commit):
    import tempfile

    sys.path.insert(0, "/opt/runtime/spreadsheet-gym-ubuntu24.04-x86_64/source")
    from resources_servers.spreadsheet.service.engines import LibreOfficePool

    run = Path("/artifacts/audit-runs") / run_id
    tasks = {row["task_id"]: row for row in map(json.loads, (run / "tasks.jsonl").read_text().splitlines())}
    result = []
    archive_path = Path("/artifacts/audit-inputs/6f274cd4cbc27e656d7ddae7c3b591669866dc84/spreadsheets.zip")
    with tempfile.TemporaryDirectory() as folder, ZipFile(archive_path) as archive:
        root = Path(folder)
        engine = LibreOfficePool(root / "office", workers=1)
        try:
            for index, task_id in enumerate(task_ids):
                path = root / f"oracle-{index}.xlsx"
                path.write_bytes(archive.read(tasks[task_id]["thread"] + "/target.xlsx"))
                record = {"task_id": task_id, "original": inspect_xml(path)}
                engine.recalculate(path)
                record["libreoffice_export"] = inspect_xml(path)
                result.append(record)
        finally:
            engine.close()
    identity = hashlib.sha256(json.dumps(task_ids).encode()).hexdigest()[:8]
    destination = run / f"focused-dependencies-{source_commit[:12]}-{identity}.json"
    destination.write_text(
        json.dumps({"tasks": result, "image_id": image.object_id, "source_commit": source_commit}, indent=2) + "\n"
    )
    volume.commit()
    return str(destination.relative_to("/artifacts"))


@app.local_entrypoint()
def main(run_id: str, task_ids: str, output_file: str):
    import subprocess

    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id) or Path(output_file).exists():
        raise ValueError("Use an existing audit run ID and a fresh local output file")
    root = Path(__file__).resolve().parents[3]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True):
        raise ValueError("Use clean committed inspection source")
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    report = inspect_tasks.remote(run_id, task_ids.split(","), source_commit)
    Path(output_file).write_bytes(b"".join(volume.read_file(report)))
    print("ORACLE_DEPENDENCY_INSPECTION_SAVED", flush=True)
