"""Private, checkpointed task audit against the frozen pre-fix reward reader.

Run with the locked Linux service Python. Oracle values/formulas in diagnostics
must stay separate from policy observations and public source artifacts.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from zipfile import ZipFile


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def dump(path, value):
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def exception_chain(error):
    result = []
    while error is not None and len(result) < 4:
        result.append({"type": type(error).__name__, "message": str(error)[:500]})
        error = error.__cause__
    return result


def prepare(data, output):
    import pyarrow.parquet as pq

    from resources_servers.spreadsheet.packaging.prepare_dataset import REVISION

    tasks, ids = [], set()
    with ZipFile(data / "spreadsheets.zip") as archive:
        members = set(archive.namelist())
        for split in ("train", "test"):
            for row in pq.read_table(data / f"{split}_hermes.parquet").to_pylist():
                task_id = row["extra_info"]["id"]
                thread = row["reward_model"]["ground_truth"]
                if task_id in ids:
                    raise ValueError("Duplicate task in pinned input")
                ids.add(task_id)
                for name in ("output.xlsx", "target.xlsx"):
                    if thread + "/" + name not in members:
                        raise ValueError("Pinned task workbook missing")
                tasks.append(
                    {
                        "task_id": task_id,
                        "thread": thread,
                        "split": split,
                        "answer_position": row["extra_info"]["answer_position"],
                        "dataset_revision": REVISION,
                    }
                )
    if len(tasks) != 8647:
        raise ValueError("Pinned dataset task count differs")
    output.write_text("".join(json.dumps(task) + "\n" for task in tasks))
    print(
        json.dumps(
            {"event": "audit_inputs_ready", "tasks": len(tasks), "sha256": sha(output)}
        ),
        flush=True,
    )


def frozen_reader(bundle):
    path = bundle / "source/resources_servers/spreadsheet/service/workbook.py"
    name = "resources_servers.spreadsheet.service._frozen_workbook"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, sha(path)


def value(value):
    return {"type": type(value).__name__, "repr": repr(value)[:250]}


def inspect_cells(oracle, actual, address):
    from resources_servers.spreadsheet.service.workbook import (
        answer_regions,
        cell_equal,
        load,
        normalize,
    )
    from openpyxl.utils import get_column_letter

    books = []
    differences, categories = [], Counter()
    digest = hashlib.sha256()
    result = {}
    try:
        original = load(oracle, values=True, read_only=True)
        books.append(original)
        produced = load(actual, values=True, read_only=True)
        books.append(produced)
        formulas = load(oracle, read_only=True)
        books.append(formulas)
        for ws, (left, top, right, bottom) in answer_regions(original, address):
            bounds = dict(
                min_row=top,
                max_row=bottom,
                min_col=left,
                max_col=right,
                values_only=True,
            )
            expected_rows = iter(ws.iter_rows(**bounds))
            formula_rows = iter(formulas[ws.title].iter_rows(**bounds))
            exists = ws.title in produced.sheetnames
            actual_rows = (
                iter(produced[ws.title].iter_rows(**bounds)) if exists else iter(())
            )
            empty = (None,) * (right - left + 1)
            for row in range(top, bottom + 1):
                expected, observed, expressions = (
                    next(expected_rows, empty),
                    next(actual_rows, empty),
                    next(formula_rows, empty),
                )
                for index in range(right - left + 1):
                    a, b = expected[index], observed[index]
                    cell = f"{get_column_letter(left + index)}{row}"
                    digest.update(
                        json.dumps([ws.title, cell, value(b)], sort_keys=True).encode()
                    )
                    if exists and cell_equal(a, b, original.epoch, produced.epoch):
                        continue
                    kind = "value_mismatch"
                    detail = {}
                    if not exists:
                        kind = "missing_sheet"
                    elif (
                        type(a) in (int, float)
                        and type(b) in (int, float)
                        and math.isfinite(a)
                        and math.isfinite(b)
                    ):
                        delta = abs(a - b)
                        unit = max(math.ulp(float(a)), math.ulp(float(b)))
                        detail = {"absolute_difference": delta, "ulps": delta / unit}
                        # Diagnosis only: this never changes reward or qualification.
                        kind = (
                            "numeric_precision_candidate"
                            if delta <= 1e-7 and delta <= 4 * unit
                            else "numeric_value_mismatch"
                        )
                    elif a is None or b is None:
                        kind = "missing_cached_value"
                    elif isinstance(a, str) and isinstance(b, str):
                        kind = (
                            "formula_error"
                            if a.startswith(
                                (
                                    "#REF!",
                                    "#NAME?",
                                    "#VALUE!",
                                    "#N/A",
                                    "#NUM!",
                                    "#DIV/0!",
                                )
                            )
                            or b.startswith(
                                (
                                    "#REF!",
                                    "#NAME?",
                                    "#VALUE!",
                                    "#N/A",
                                    "#NUM!",
                                    "#DIV/0!",
                                )
                            )
                            else "text_mismatch"
                        )
                    categories[kind] += 1
                    if len(differences) < 20:
                        differences.append(
                            {
                                "sheet": ws.title,
                                "cell": cell,
                                "kind": kind,
                                "formula": value(expressions[index]),
                                "original_cached": value(a),
                                "recalculated": value(b),
                                "normalized_original": value(
                                    normalize(a, original.epoch)
                                ),
                                "normalized_actual": value(
                                    normalize(b, produced.epoch)
                                ),
                                **detail,
                            }
                        )
        result.update(
            difference_counts=dict(categories),
            differences=differences,
            values_sha256=digest.hexdigest(),
        )
    except Exception as error:
        result["inspection_error"] = exception_chain(error)
    finally:
        for book in books:
            book.close()
    return result


def tool_roundtrip(actual, oracle, address, engine, temporary):
    """A scoring-only recovery must not be silently promoted as tool-ready."""
    from resources_servers.spreadsheet.schemas import TOOL_MODELS
    from resources_servers.spreadsheet.service.workbook import compare, execute, load

    path = temporary / "native-roundtrip.xlsx"
    shutil.copyfile(actual, path)
    try:
        wb = load(path)
        try:
            name = "__Audit_QA__"
            while name in wb.sheetnames:
                name += "_"
        finally:
            wb.close()

        def call(tool, **arguments):
            execute(
                path,
                tool,
                TOOL_MODELS[tool].model_validate(arguments).model_dump(by_alias=True),
            )

        call("manage_sheet", action="create", sheet_name=name)
        call("write_range", sheet_name=name, range="A1", data=123)
        call("manage_sheet", action="delete", sheet_name=name)
        engine.recalculate(path)
        wb = load(path)
        wb.close()
        score = compare(oracle, path, address)
        return {"passed": score["reward"] == 1, "score": score}
    except Exception as error:
        return {"passed": False, "error": exception_chain(error)}


def audit_task(task, archive, baseline, engine, root):
    from resources_servers.spreadsheet.service.workbook import compare

    record = {
        **task,
        "baseline_qualified": False,
        "strict_qualified": False,
        "attempts": [],
    }
    started = time.monotonic()
    original_bytes = archive.read(task["thread"] + "/target.xlsx")
    record["oracle_sha256"] = hashlib.sha256(original_bytes).hexdigest()
    if task.get("previous_oracle_sha256", record["oracle_sha256"]) != record["oracle_sha256"]:
        raise RuntimeError("Oracle differs from pinned previous audit")
    with tempfile.TemporaryDirectory(prefix="task-", dir=root) as folder:
        temporary = Path(folder)
        oracle = temporary / "original.xlsx"
        oracle.write_bytes(original_bytes)
        baseline_alive = True
        last_actual = None
        for index in range(2):
            actual = temporary / f"recalculated-{index}.xlsx"
            actual.write_bytes(original_bytes)
            attempt = {"index": index}
            stage = "recalculate"
            try:
                try:
                    calculation = engine.recalculate(actual)
                    if calculation:
                        attempt.update(calculation)
                except Exception as first_error:
                    # Retry only the preidentified timeout cohort, from pristine
                    # bytes after the engine has killed its failed worker.
                    if task.get("candidate_group") != "libreoffice_timeout" or "timed out" not in str(first_error):
                        raise
                    attempt["initial_timeout"] = True
                    previous_timeout = engine.timeout
                    engine.timeout = 600
                    try:
                        actual.write_bytes(original_bytes)
                        engine.recalculate(actual)
                        attempt["required_timeout_seconds"] = 600
                    finally:
                        engine.timeout = previous_timeout
                last_actual = actual
            except Exception as error:
                attempt.update(error_stage=stage, error_chain=exception_chain(error))
                baseline_alive = False
                record["attempts"].append(attempt)
                continue
            if baseline_alive:
                try:
                    attempt["baseline"] = baseline.compare(
                        oracle, actual, task["answer_position"]
                    )
                    baseline_alive = attempt["baseline"]["reward"] == 1
                    if not baseline_alive:
                        record["baseline_reason"] = "oracle_value_mismatch"
                except Exception as error:
                    attempt["baseline_error"] = exception_chain(error)
                    record["baseline_reason"] = "recalculation_or_comparison_failed"
                    baseline_alive = False
            try:
                attempt["strict"] = compare(oracle, actual, task["answer_position"])
            except Exception as error:
                attempt.update(
                    error_stage="strict_compare", error_chain=exception_chain(error)
                )
            record["attempts"].append(attempt)
        record["baseline_qualified"] = baseline_alive
        record["strict_qualified"] = all(
            a.get("strict", {}).get("reward") == 1 for a in record["attempts"]
        )
        if not record["baseline_qualified"]:
            record.setdefault("baseline_reason", "recalculation_or_comparison_failed")
            if last_actual is not None:
                # Compare both independent recalculations to diagnose volatility.
                inspections = [
                    inspect_cells(
                        oracle,
                        temporary / f"recalculated-{i}.xlsx",
                        task["answer_position"],
                    )
                    for i in range(2)
                    if (temporary / f"recalculated-{i}.xlsx").exists()
                    and "strict" in record["attempts"][i]
                ]
                if inspections:
                    record["inspection"] = inspections[0]
                    hashes = [item.get("values_sha256") for item in inspections]
                    record["stable_recalculations"] = (
                        len(hashes) == 2
                        and None not in hashes
                        and len(set(hashes)) == 1
                    )
            if record["strict_qualified"]:
                record["native_roundtrip"] = tool_roundtrip(
                    last_actual, oracle, task["answer_position"], engine, temporary
                )
                record["seed_sha256"] = hashlib.sha256(
                    archive.read(task["thread"] + "/output.xlsx")
                ).hexdigest()
        if task.get("candidate_group"):
            from resources_servers.spreadsheet.service.workbook import answer_regions, load
            from resources_servers.spreadsheet.packaging.recovery_cohort import negative_control, seed_roundtrip

            book = None
            try:
                book = load(oracle, values=True, read_only=True)
                record["canonical_regions"] = [
                    {"sheet": ws.title, "bounds": bounds}
                    for ws, bounds in answer_regions(book, task["answer_position"])
                ]
            except Exception as error:
                record["metadata_error"] = exception_chain(error)
            finally:
                if book is not None:
                    book.close()
            if record["strict_qualified"]:
                if "native_roundtrip" not in record:
                    record["native_roundtrip"] = tool_roundtrip(
                        last_actual, oracle, task["answer_position"], engine, temporary
                    )
                try:
                    record["negative_control"] = negative_control(
                        oracle, last_actual, task["answer_position"], temporary / "wrong.xlsx"
                    )
                except Exception as error:
                    record["negative_control"] = {"passed": False, "error": exception_chain(error)}
                seed = temporary / "seed.xlsx"
                seed_bytes = archive.read(task["thread"] + "/output.xlsx")
                seed.write_bytes(seed_bytes)
                record["seed_sha256"] = hashlib.sha256(seed_bytes).hexdigest()
                record["seed_roundtrip"] = seed_roundtrip(seed, engine)
        if sha(oracle) != record["oracle_sha256"]:
            raise RuntimeError("Immutable oracle changed")
    if record["baseline_qualified"]:
        record["verdict"] = (
            "baseline_retained" if record["strict_qualified"] else "strict_regression"
        )
    elif record["strict_qualified"]:
        record["verdict"] = (
            "recovered_strict"
            if record["native_roundtrip"]["passed"]
            else "recovered_scoring_only"
        )
    elif record.get("stable_recalculations") is False:
        record["verdict"] = "unstable_or_unconfirmed_recalculation"
    else:
        counts = record.get("inspection", {}).get("difference_counts", {})
        record["verdict"] = (
            next(iter(counts))
            if len(counts) == 1
            else "mixed_value_mismatch"
            if counts
            else "unresolved_error"
        )
    if task.get("candidate_group") and record["strict_qualified"]:
        record["verdict"] = "recovered_strict" if all(
            record.get(gate, {}).get("passed")
            for gate in ("native_roundtrip", "seed_roundtrip", "negative_control")
        ) else "recovered_scoring_only"
    record["seconds"] = round(time.monotonic() - started, 3)
    return record


def batch(data, bundle, tasks_path, output, start, size, source_commit):
    from resources_servers.spreadsheet.service.engines import LibreOfficePool

    tasks = [json.loads(line) for line in tasks_path.read_text().splitlines()][
        start : start + size
    ]
    if not tasks:
        raise ValueError("Empty batch")
    baseline, baseline_sha = frozen_reader(bundle)
    identity = {
        "source_commit": source_commit,
        "tasks_sha256": sha(tasks_path),
        "baseline_reader_sha256": baseline_sha,
        "start": start,
        "size": size,
        "task_ids": [task["task_id"] for task in tasks],
    }
    if (output / "complete.json").exists():
        saved = json.loads((output / "complete.json").read_text())
        if saved["identity"] != identity or saved["records_sha256"] != sha(
            output / "records.jsonl"
        ):
            raise ValueError("Checkpoint differs from requested audit")
        print(json.dumps({"event": "audit_batch_reused", "start": start}), flush=True)
        return
    output.mkdir(parents=True, exist_ok=True)
    records = []
    with (
        tempfile.TemporaryDirectory(prefix="spreadsheet-audit-") as folder,
        ZipFile(data / "spreadsheets.zip") as archive,
    ):
        root = Path(folder)
        engine = LibreOfficePool(root / "office", workers=1, timeout=120)
        try:
            engine.probe()
            if engine.version != "LibreOffice 24.2.7.2 420(Build:2)":
                raise ValueError(
                    "Audit LibreOffice version differs from qualified runtime"
                )
            for index, task in enumerate(tasks):
                record = audit_task(task, archive, baseline, engine, root)
                records.append(record)
                if (index + 1) % 10 == 0:
                    print(
                        json.dumps(
                            {
                                "event": "audit_batch_progress",
                                "start": start,
                                "done": index + 1,
                            }
                        ),
                        flush=True,
                    )
        finally:
            engine.close()
    target = output / "records.jsonl"
    temporary = target.with_suffix(".partial")
    temporary.write_text(
        "".join(json.dumps(record, default=str) + "\n" for record in records)
    )
    temporary.replace(target)
    dump(
        output / "complete.json",
        {
            "identity": identity,
            "records_sha256": sha(target),
            "counts": dict(Counter(r["verdict"] for r in records)),
        },
    )
    print(
        json.dumps(
            {"event": "audit_batch_done", "start": start, "tasks": len(records)}
        ),
        flush=True,
    )


def summarize(run, data):
    import pyarrow.parquet as pq
    from resources_servers.spreadsheet.prepare import SYSTEM_PROMPT
    from resources_servers.spreadsheet.schemas import tool_schemas

    records, seen = [], set()
    for path in sorted((run / "batches").glob("*/complete.json")):
        checkpoint = json.loads(path.read_text())
        target = path.with_name("records.jsonl")
        if sha(target) != checkpoint["records_sha256"]:
            raise ValueError("Audit checkpoint hash differs")
        for line in target.read_text().splitlines():
            record = json.loads(line)
            if record["task_id"] in seen:
                raise ValueError("Duplicate audit task")
            seen.add(record["task_id"])
            records.append(record)
    expected = json.loads((run / "identity.json").read_text())
    if len(records) != expected["tasks"]:
        raise ValueError("Incomplete audit")
    recovered = [
        record for record in records if record["verdict"] == "recovered_strict"
    ]
    rejected = [record for record in records if not record["baseline_qualified"]]
    (run / "rejected.audit.jsonl").write_text(
        "".join(json.dumps(record, default=str) + "\n" for record in rejected)
    )
    manifests = [
        {
            "task_id": r["task_id"],
            "seed": r["thread"] + "/output.xlsx",
            "oracle": r["thread"] + "/target.xlsx",
            "answer_position": r["answer_position"],
            "dataset_revision": r["dataset_revision"],
            "seed_sha256": r["seed_sha256"],
            "oracle_sha256": r["oracle_sha256"],
            "libreoffice_qualified": True,
            "qualified_engine": "LibreOffice 24.2.7.2 420(Build:2)",
            "qualification_profile": "strict_values_streaming_reader_v2",
            "audit_source_commit": expected["source_commit"],
        }
        for r in recovered
    ]
    (run / "recovered.manifest.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in manifests)
    )
    for split in ("train", "test"):
        ids = {r["task_id"] for r in recovered if r["split"] == split}
        rows = []
        for row in pq.read_table(data / f"{split}_hermes.parquet").to_pylist():
            if row["extra_info"]["id"] in ids:
                rows.append(
                    {
                        "agent_ref": {
                            "name": "spreadsheet_agent",
                            "type": "responses_api_agents",
                        },
                        "task_id": row["extra_info"]["id"],
                        "dataset_revision": expected["dataset_revision"],
                        "responses_create_params": {
                            "input": [{"role": "system", "content": SYSTEM_PROMPT}]
                            + [
                                message
                                for message in row["prompt"]
                                if message["role"] == "user"
                            ],
                            "tools": tool_schemas(),
                            "parallel_tool_calls": False,
                        },
                    }
                )
        (run / f"recovered.{split}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        )
    summary = {
        "identity": expected,
        "checked": len(records),
        "baseline_qualified": len(records) - len(rejected),
        "baseline_rejected": len(rejected),
        "historical_operator_qualified": 7684,
        "historical_membership_verified": False,
        "historical_membership_note": "Original per-task node report unavailable; frozen code/runtime baseline reproduced independently",
        "verdicts": dict(Counter(record["verdict"] for record in records)),
        "baseline_reasons": dict(
            Counter(record["baseline_reason"] for record in rejected)
        ),
        "recovered_by_split": dict(Counter(record["split"] for record in recovered)),
        "strict_regressions": [
            record["task_id"]
            for record in records
            if record["verdict"] == "strict_regression"
        ],
        "files": {p.name: sha(p) for p in run.glob("*.jsonl")},
        "policy": "Strict original cell equality; precision candidates are diagnostic only. No oracle replacement or live deployment.",
    }
    dump(run / "summary.json", summary)
    print(
        json.dumps(
            {
                "event": "audit_complete",
                "checked": len(records),
                "verdicts": summary["verdicts"],
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "batch", "summarize"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--start", type=int)
    parser.add_argument("--size", type=int)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.data, args.output)
    elif args.mode == "batch":
        batch(
            args.data,
            args.bundle,
            args.tasks,
            args.output,
            args.start,
            args.size,
            args.source_commit,
        )
    else:
        summarize(args.output, args.data)


if __name__ == "__main__":
    main()
