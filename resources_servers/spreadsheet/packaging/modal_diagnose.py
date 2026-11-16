"""Reproduce public qualification samples on the existing CPU runtime image."""

import json
import sys

import modal

from resources_servers.spreadsheet.packaging.modal_build import image


app = modal.App("spreadsheet-gym-oracle-diagnostics")
REVISION = "6f274cd4cbc27e656d7ddae7c3b591669866dc84"
SAMPLES = {
    "excelforum/excel-general/task-job-933b98af516f8ec6": "oracle_value_mismatch",
    "excelforum/excel-general/task-job-88a50828e2e93fe2": "oracle_value_mismatch",
    "excelforum/excel-general/task-job-c16619b2fd429d2b": "oracle_value_mismatch",
    "excelforum/excel-general/task-job-2b958cc2164ef428": "recalculation_or_comparison_failed",
    "excelforum/excel-general/task-job-80714e25138d64ed": "recalculation_or_comparison_failed",
    "excelforum/excel-general/task-job-6684c7399a90d849": "recalculation_or_comparison_failed",
}


@app.function(image=image, cpu=2, memory=8192, timeout=1200)
def diagnose():
    import hashlib
    import shutil
    import tempfile
    import time
    import zipfile
    from pathlib import Path

    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    sys.path.insert(0, "/source")
    from resources_servers.spreadsheet.service.engines import LibreOfficePool
    from resources_servers.spreadsheet.service.workbook import compare, load, normalize, resolve, split_ranges

    def emit(**record):
        print("ORACLE_DIAGNOSTIC=" + json.dumps(record, default=str), flush=True)

    def show(value):
        return {"type": type(value).__name__, "repr": repr(value)[:300]}

    def chain(exc):
        result = []
        while exc is not None and len(result) < 3:
            result.append({"type": type(exc).__name__, "message": str(exc)[:500]})
            exc = exc.__cause__
        return result

    rows = {}
    for filename in ("train_hermes.parquet", "test_hermes.parquet"):
        path = hf_hub_download("Spreadsheet-RL/Spreadsheet-RL", filename, repo_type="dataset", revision=REVISION)
        for row in pq.read_table(path).to_pylist():
            if row["extra_info"]["id"] in SAMPLES:
                rows[row["extra_info"]["id"]] = row
    if set(rows) != set(SAMPLES):
        raise RuntimeError("A sample task was absent from the pinned parquets")
    emit(phase="download_archive", samples=len(rows), revision=REVISION)
    path = Path(
        hf_hub_download("Spreadsheet-RL/Spreadsheet-RL", "spreadsheets.zip", repo_type="dataset", revision=REVISION)
    )
    with path.open("rb") as stream:
        if (
            hashlib.file_digest(stream, "sha256").hexdigest()
            != "d69a7ebc62cb3bd0c7e0430124f6c526e2b66696c32f99c172d5b6169db2f276"
        ):
            raise RuntimeError("Archive checksum mismatch")
    output = []
    with tempfile.TemporaryDirectory(prefix="oracle-diagnostic-") as temporary, zipfile.ZipFile(path) as archive:
        directory = Path(temporary)
        engine = LibreOfficePool(directory / "workers", workers=1, timeout=120)
        try:
            engine.probe()
            emit(phase="engine", version=engine.version)
            for index, (task_id, original_reason) in enumerate(SAMPLES.items()):
                row = rows[task_id]
                member = row["reward_model"]["ground_truth"] + "/target.xlsx"
                oracle, actual = directory / f"original-{index}.xlsx", directory / f"copy-{index}.xlsx"
                oracle.write_bytes(archive.read(member))
                expected_sha = hashlib.sha256(oracle.read_bytes()).hexdigest()
                stage = "read_oracle_and_metadata"
                record = {
                    "task_id": task_id,
                    "original_reason": original_reason,
                    "answer_position": row["extra_info"]["answer_position"],
                    "engine": engine.version,
                }
                opened = []
                started = time.monotonic()
                emit(phase="sample_start", task_id=task_id)
                try:
                    expected = load(oracle, values=True)
                    opened.append(expected)
                    ranges = [resolve(expected, address) for address in split_ranges(record["answer_position"])]
                    record["sheets"] = expected.sheetnames
                    for attempt in range(2):
                        shutil.copyfile(oracle, actual)
                        stage = "recalculate"
                        timer = time.monotonic()
                        engine.recalculate(actual)
                        record.setdefault("recalculate_seconds", []).append(round(time.monotonic() - timer, 3))
                        stage = "compare"
                        score = compare(oracle, actual, record["answer_position"])
                        record["score"] = score
                        if score["reward"] != 1:
                            break
                    stage = "inspect_differences"
                    produced, formulas = load(actual, values=True), load(oracle)
                    opened.extend([produced, formulas])
                    differences = []
                    for ws, (left, top, right, bottom) in ranges:
                        for cells in ws.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right):
                            for cell in cells:
                                exists = ws.title in produced.sheetnames
                                other = produced[ws.title][cell.coordinate] if exists else None
                                a, b = cell.value, other.value if other is not None else None
                                na, nb = normalize(a, expected.epoch), normalize(b, produced.epoch)
                                exact = type(a) is type(b) and type(a) in (str, float, int, bool) and a == b
                                if exists and (exact or (type(na) is type(nb) and na == nb)):
                                    continue
                                if len(differences) < 5:
                                    differences.append(
                                        {
                                            "sheet": ws.title,
                                            "cell": cell.coordinate,
                                            "formula": show(formulas[ws.title][cell.coordinate].value),
                                            "original_cached": show(a),
                                            "lo_cached": show(b),
                                            "normalized_original": show(na),
                                            "normalized_lo": show(nb),
                                            "original_cell_type": cell.data_type,
                                            "lo_cell_type": other.data_type if other is not None else "missing_sheet",
                                        }
                                    )
                    record["differences"] = differences
                except Exception as exc:
                    record.update(error_stage=stage, error_chain=chain(exc))
                finally:
                    for workbook in opened:
                        workbook.close()
                if hashlib.sha256(oracle.read_bytes()).hexdigest() != expected_sha:
                    raise RuntimeError("Original oracle was modified")
                record["seconds"] = round(time.monotonic() - started, 3)
                output.append(record)
                emit(phase="sample_result", **record)
        finally:
            engine.close()
    return {"dataset_revision": REVISION, "engine": engine.version, "samples": output}


@app.local_entrypoint()
def main():
    result = diagnose.remote()
    print("ORACLE_DIAGNOSTIC_SUMMARY=" + json.dumps(result, default=str), flush=True)
