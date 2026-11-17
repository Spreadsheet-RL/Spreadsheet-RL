"""Private follow-up audit; never replaces active data or emits training rows."""

import hashlib
import json
import posixpath
from collections import Counter
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from resources_servers.spreadsheet.packaging.audit_rejections import (
    dump,
    exception_chain,
    sha,
)


GROUPS = {
    "Use a finite A1 cell or rectangle": "finite_A1_parser",
    "Worksheet does not exist": "worksheet_resolution",
    "At most 100000 cells per range; split the request": "per_range_cell_limit",
    "LibreOffice worker timed out": "libreoffice_timeout",
}
EXPECTED_GROUPS = {
    "finite_A1_parser": 69,
    "worksheet_resolution": 22,
    "per_range_cell_limit": 10,
    "libreoffice_timeout": 3,
    "native_reader": 21,
}
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_records(run):
    records = []
    hashes = {}
    for checkpoint in sorted((run / "batches").glob("*/complete.json")):
        saved = json.loads(checkpoint.read_text())
        path = checkpoint.with_name("records.jsonl")
        digest = sha(path)
        if digest != saved["records_sha256"]:
            raise ValueError("Previous audit checkpoint differs")
        hashes[str(checkpoint.relative_to(run))] = sha(checkpoint)
        records.extend(json.loads(line) for line in path.read_text().splitlines())
    if len({row["task_id"] for row in records}) != len(records):
        raise ValueError("Duplicate audited task")
    return records, hashes


def candidate_group(record):
    if record["verdict"] == "recovered_scoring_only":
        return "native_reader"
    if record["verdict"] == "unresolved_error":
        chain = record["attempts"][0].get("error_chain", [])
        if chain:
            return GROUPS.get(chain[0]["message"])
    return None


def prepare_cohort(tasks_path, previous):
    records, hashes = read_records(previous)
    if len(records) != 8647:
        raise ValueError("Expected complete pinned previous audit")
    prior = {row["task_id"]: row for row in records}
    tasks = [json.loads(line) for line in tasks_path.read_text().splitlines()]
    groups = Counter()
    for task in tasks:
        old = prior[task["task_id"]]
        for key in ("thread", "split", "answer_position", "dataset_revision"):
            if task[key] != old[key]:
                raise ValueError("Previous audit task identity changed")
        group = candidate_group(old)
        groups.update([group] if group else [])
        task.update(
            previous_verdict=old["verdict"],
            candidate_group=group,
            previous_oracle_sha256=old["oracle_sha256"],
        )
    if dict(groups) != EXPECTED_GROUPS or len(tasks) != len(prior):
        raise ValueError("Previous 125-task cohort membership differs")
    tasks_path.write_text("".join(json.dumps(task) + "\n" for task in tasks))
    return {
        "run_id": previous.name,
        "summary_sha256": sha(previous / "summary.json"),
        "checkpoint_sha256": hashes,
        "candidate_groups": dict(groups),
        "baseline_retained": sum(r["verdict"] == "baseline_retained" for r in records),
    }


def content_digest(path):
    """Hash logical cells/formulas/types without copying the full workbook into RAM."""
    from resources_servers.spreadsheet.service.workbook import load

    book = load(path, read_only=True)
    digest = hashlib.sha256()
    count = 0
    try:
        digest.update(str(book.epoch).encode())
        for sheet in book:
            digest.update(json.dumps([sheet.title, sheet.sheet_state]).encode())
            for row in sheet:
                for cell in row:
                    count += 1
                    if count > 8_000_000:
                        raise ValueError("Seed content scan budget exceeded")
                    if cell.value is not None:
                        digest.update(
                            json.dumps(
                                [
                                    cell.coordinate,
                                    cell.data_type,
                                    type(cell.value).__name__,
                                    str(cell.value),
                                ]
                            ).encode()
                        )
        return digest.hexdigest()
    finally:
        book.close()


def seed_roundtrip(seed, engine):
    from resources_servers.spreadsheet.schemas import TOOL_MODELS
    from resources_servers.spreadsheet.service.workbook import execute, load

    try:
        before = content_digest(seed)
        wb = load(seed)
        try:
            name = "__Audit_Seed__"
            while name in wb.sheetnames:
                name += "_"
        finally:
            wb.close()
        for tool, args in [
            ("manage_sheet", {"action": "create", "sheet_name": name}),
            ("write_range", {"sheet_name": name, "range": "A1", "data": 321}),
            ("manage_sheet", {"action": "delete", "sheet_name": name}),
        ]:
            execute(
                seed,
                tool,
                TOOL_MODELS[tool].model_validate(args).model_dump(by_alias=True),
            )
        unchanged = before == content_digest(seed)
        engine.recalculate(seed)
        wb = load(seed)
        wb.close()
        return {"passed": unchanged, "logical_cells_and_formulas_preserved": unchanged}
    except Exception as error:
        return {"passed": False, "error": exception_chain(error)}


def negative_control(oracle, actual, address, output):
    """Mutate a single scored cell in OOXML, without relying on the editing reader."""
    from openpyxl.utils import get_column_letter
    from resources_servers.spreadsheet.service.workbook import (
        answer_regions,
        compare,
        load,
    )

    wb = load(oracle, values=True, read_only=True)
    try:
        sheet, (left, top, _, _) = answer_regions(wb, address)[0]
        sheet_name = sheet.title
        coordinate = f"{get_column_letter(left)}{top}"
    finally:
        wb.close()
    with ZipFile(actual) as archive:
        book = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = list(book.find(NS + "sheets"))
        info = next(s for s in sheets if s.attrib["name"] == sheet_name)
        rel = info.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = next(
            r.attrib["Target"] for r in relationships if r.attrib["Id"] == rel
        )
        part = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath("xl/" + target)
        )
        tree = ET.fromstring(archive.read(part))
        data = tree.find(NS + "sheetData")
        row = next((r for r in data if int(r.attrib["r"]) == top), None)
        if row is None:
            row = ET.Element(NS + "row", r=str(top))
            index = next(
                (i for i, r in enumerate(data) if int(r.attrib["r"]) > top), len(data)
            )
            data.insert(index, row)
        cell = next((c for c in row if c.attrib.get("r") == coordinate), None)
        if cell is None:
            cell = ET.SubElement(row, NS + "c", r=coordinate)
            from openpyxl.utils.cell import coordinate_to_tuple

            row[:] = sorted(row, key=lambda c: coordinate_to_tuple(c.attrib["r"])[1])
        cell.clear()
        cell.attrib.update(r=coordinate, t="inlineStr")
        inline = ET.SubElement(cell, NS + "is")
        # This string cannot equal a numeric result; if an oracle contains this
        # exact sentinel, append until it differs under the unchanged comparator.
        from resources_servers.spreadsheet.service.workbook import cell_equal

        book_values = load(oracle, values=True, read_only=True)
        try:
            expected = book_values[sheet_name][coordinate].value
            sentinel = "__STRICT_AUDIT_WRONG_ANSWER__"
            while cell_equal(expected, sentinel, book_values.epoch, book_values.epoch):
                sentinel += "_"
        finally:
            book_values.close()
        ET.SubElement(inline, NS + "t").text = sentinel
        with ZipFile(output, "w") as written:
            for entry in archive.infolist():
                written.writestr(
                    entry,
                    ET.tostring(tree)
                    if entry.filename == part
                    else archive.read(entry),
                )
    score = compare(oracle, output, address)
    return {"passed": score["reward"] == 0, "reward": score["reward"]}


def summarize_cohort(run):
    records, hashes = read_records(run)
    identity = json.loads((run / "identity.json").read_text())
    tasks = [
        json.loads(line) for line in (run / "tasks.jsonl").read_text().splitlines()
    ]
    if {r["task_id"] for r in records} != {r["task_id"] for r in tasks} or len(
        records
    ) != 8647:
        raise ValueError("Incomplete recovery/regression coverage")
    candidates = [r for r in records if r.get("candidate_group")]
    groups = {}
    for name, expected_count in EXPECTED_GROUPS.items():
        rows = [r for r in candidates if r["candidate_group"] == name]
        if len(rows) != expected_count:
            raise ValueError("Candidate group membership changed")
        groups[name] = {
            "checked": len(rows),
            "verdicts": dict(Counter(r["verdict"] for r in rows)),
            "strict_values_passed": sum(r["strict_qualified"] for r in rows),
            "native_passed": sum(
                r.get("native_roundtrip", {}).get("passed", False) for r in rows
            ),
            "seed_passed": sum(
                r.get("seed_roundtrip", {}).get("passed", False) for r in rows
            ),
            "negative_controls_passed": sum(
                r.get("negative_control", {}).get("passed", False) for r in rows
            ),
        }
    regressions = [
        r["task_id"]
        for r in records
        if r["previous_verdict"] == "baseline_retained" and not r["strict_qualified"]
    ]
    changed_oracles = [
        r["task_id"]
        for r in records
        if r["oracle_sha256"] != r["previous_oracle_sha256"]
    ]
    if changed_oracles:
        raise ValueError("Original oracle bytes differ from previous audit")
    (run / "candidate.audit.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in candidates)
    )
    recovered = [r for r in candidates if r["verdict"] == "recovered_strict"]
    result = {
        "identity": identity,
        "checked": len(records),
        "candidates_checked": len(candidates),
        "candidate_groups": groups,
        "strict_recovered": len(recovered),
        "recovered_by_split": dict(Counter(r["split"] for r in recovered)),
        "previous_baseline_checked": sum(
            r["previous_verdict"] == "baseline_retained" for r in records
        ),
        "previous_baseline_regressions": regressions,
        "oracle_bytes_unchanged": not changed_oracles,
        "verdicts": dict(Counter(r["verdict"] for r in records)),
        "baseline_qualified": sum(r["baseline_qualified"] for r in records),
        "baseline_rejected": sum(not r["baseline_qualified"] for r in records),
        "checkpoint_sha256": hashes,
        "policy": "Original equality and oracle bytes; two independent recalculations, native and seed roundtrips, wrong-answer control. Diagnostic output only; no training data export or live deployment.",
    }
    dump(run / "summary.json", result)
    return result
