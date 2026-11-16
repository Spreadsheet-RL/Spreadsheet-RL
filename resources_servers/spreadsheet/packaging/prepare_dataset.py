"""Prepare the pinned public dataset and checkpoint full LibreOffice qualification.

Run with the installed service Python and PYTHONPATH pointing at bundled source.
This helper downloads nothing and does not start the service or modify oracles.
"""

import argparse
import fcntl
import hashlib
import json
import shutil
import signal
import stat
import tempfile
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath


REVISION = "6f274cd4cbc27e656d7ddae7c3b591669866dc84"
FILES = {
    "spreadsheets.zip": (1221095226, "d69a7ebc62cb3bd0c7e0430124f6c526e2b66696c32f99c172d5b6169db2f276"),
    "train_hermes.parquet": (3551543, "e7537324ee79cfbbd9598b21b71d6ec865701630682e3929f8784abb1d53cfe6"),
    "test_hermes.parquet": (439551, "14c8d7e07497653e32f136453a14b3c6d6d34c4f9546621ea9b7110605b47972"),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def extract_archive(archive, destination, archive_sha):
    """Publish a complete extraction atomically; interrupted staging stays separate."""
    receipt = {"archive_sha256": archive_sha}
    if destination.exists():
        if read_json(destination / ".extraction.json") != receipt:
            raise ValueError("Existing extraction has a different receipt")
        return
    with zipfile.ZipFile(archive) as source:
        members, names = [], set()
        for member in source.infolist():
            name = PurePosixPath(member.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in member.filename:
                raise ValueError("Unsafe archive path")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Archive symlinks are not supported")
            if member.is_dir() or name.name == ".DS_Store" or "__MACOSX" in name.parts:
                continue
            if str(name) in names or not name.parts:
                raise ValueError("Duplicate or empty archive path")
            names.add(str(name))
            members.append(member)
        needed = sum(member.file_size for member in members)
        if shutil.disk_usage(destination.parent).free < needed + 1024**3:
            raise ValueError("Not enough free disk for extraction plus 1 GiB")
        stage = Path(tempfile.mkdtemp(prefix=".extract-SP06-", dir=destination.parent))
        emit("extract_start", files=len(members), uncompressed_bytes=needed)
        for index, member in enumerate(members, 1):
            source.extract(member, stage)  # ZipFile validates CRC as each file is read.
            if index % 2000 == 0:
                emit("extract_progress", done=index, total=len(members))
        write_json(stage / ".extraction.json", receipt)
        stage.rename(destination)


def find_data_root(extracted, threads):
    threads = list(dict.fromkeys(threads))
    for thread in threads:
        name = PurePosixPath(thread)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("Unsafe task path")
    candidates = [extracted] + sorted(path for path in extracted.iterdir() if path.is_dir())
    # Discover the optional archive wrapper; prepare() validates every task below it.
    probes = threads[:16] + threads[-16:]
    matches = [
        root for root in candidates if probes and all((root / thread / "output.xlsx").is_file() for thread in probes)
    ]
    if len(matches) != 1:
        raise ValueError("Cannot uniquely locate the dataset root in the archive")
    return matches[0]


def prepare_inputs(download, extracted, prepared, identity):
    if prepared.exists():
        if read_json(prepared / "node-preparation.json") != identity:
            raise ValueError("Existing prepared data belongs to a different run")
        return
    import pyarrow.parquet as pq

    from resources_servers.spreadsheet.prepare import prepare

    parquets = [download / name for name in ("train_hermes.parquet", "test_hermes.parquet")]
    threads = [
        row["reward_model"]["ground_truth"] for parquet in parquets for row in pq.read_table(parquet).to_pylist()
    ]
    data_root = find_data_root(extracted, threads)
    stage = Path(tempfile.mkdtemp(prefix=".prepare-SP06-", dir=prepared.parent))
    count = prepare(parquets, data_root, stage, REVISION)
    write_json(stage / "node-preparation.json", identity)
    write_json(stage / "data-root.json", {"data_root": str(data_root)})
    stage.rename(prepared)
    emit("prepared", tasks=count, data_root=str(data_root))


def qualify_batches(manifest, data_root, batches, engine, batch_size, helper_sha):
    from resources_servers.spreadsheet.qualify import qualify

    lines = manifest.read_text().splitlines(keepends=True)
    batches.mkdir(exist_ok=True)
    accepted, diagnostics = [], []
    for start in range(0, len(lines), batch_size):
        payload = "".join(lines[start : start + batch_size])
        source_hash = hashlib.sha256(payload.encode()).hexdigest()
        identity = {"input_sha256": source_hash, "engine": engine.version, "helper_sha256": helper_sha}
        final = batches / f"{start:06d}"
        resumed = final.exists()
        if not resumed:
            emit("qualify_batch_start", done=start, total=len(lines))
            stage = Path(tempfile.mkdtemp(prefix=f".{start:06d}-", dir=batches))
            source = stage / "input.jsonl"
            source.write_text(payload)
            qualify(source, data_root, stage / "accepted.jsonl", stage / "report.json", engine)
            write_json(stage / "checkpoint.json", identity)
            stage.rename(final)
        if read_json(final / "checkpoint.json") != identity:
            raise ValueError("Qualification checkpoint contract changed; use a fresh output directory")
        report = read_json(final / "report.json")
        selected = [json.loads(line) for line in (final / "accepted.jsonl").read_text().splitlines()]
        expected_ids = [json.loads(line)["task_id"] for line in payload.splitlines()]
        if (
            report["source_manifest_sha256"] != source_hash
            or report["engine"] != engine.version
            or report["total"] != len(expected_ids)
            or report["qualified"] != len(selected)
            or [task["task_id"] for task in report["tasks"]] != expected_ids
            or [task["task_id"] for task in selected]
            != [task["task_id"] for task in report["tasks"] if task["qualified"]]
            or any(not task["libreoffice_qualified"] for task in selected)
        ):
            raise ValueError("Qualification checkpoint is inconsistent")
        accepted.extend(selected)
        diagnostics.extend(report["tasks"])
        emit("qualify_progress", done=len(diagnostics), total=len(lines), qualified=len(accepted), resumed=resumed)
    return accepted, diagnostics


def publish_results(prepared, accepted, diagnostics, identity):
    destination = prepared / "libreoffice"
    if destination.exists():
        if read_json(destination / "completion.json") != identity:
            raise ValueError("Existing qualification results have a different contract")
        return destination
    stage = Path(tempfile.mkdtemp(prefix=".results-SP06-", dir=prepared))
    (stage / "manifest.qualified.jsonl").write_text("".join(json.dumps(row) + "\n" for row in accepted))
    reasons = dict(Counter(row.get("reason", "qualified") for row in diagnostics))
    write_json(
        stage / "compatibility.json",
        {
            **identity,
            "qualified": len(accepted),
            "total": len(diagnostics),
            "reasons": reasons,
            "tasks": diagnostics,
        },
    )
    allowed = {row["task_id"] for row in accepted}
    for split in ("train", "test"):
        rows = [json.loads(line) for line in (prepared / f"{split}_hermes.jsonl").read_text().splitlines()]
        if any(row["dataset_revision"] != REVISION for row in rows):
            raise ValueError("Dataset and catalog revision mismatch")
        kept = [row for row in rows if row["task_id"] in allowed]
        (stage / f"{split}.qualified.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept)
        )
        emit("filtered", split=split, kept=len(kept), excluded=len(rows) - len(kept))
    write_json(stage / "completion.json", identity)
    stage.rename(destination)
    return destination


def run(download, work_root, batch_size):
    from resources_servers.spreadsheet.service.catalog import Catalog
    from resources_servers.spreadsheet.service.engines import LibreOfficePool

    download, work_root = download.resolve(), work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    lock = (work_root / "prepare.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    temporary = work_root / "tmp"
    temporary.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary)
    for name, (size, sha) in FILES.items():
        path = download / name
        if path.stat().st_size != size or digest(path) != sha:
            raise ValueError(f"Download checksum mismatch: {name}")
        emit("download_verified", file=name)
    extracted = download / "workbooks"
    extract_archive(download / "spreadsheets.zip", extracted, FILES["spreadsheets.zip"][1])
    helper_sha = digest(Path(__file__))
    prepared = download / "prepared-SP06"
    preparation_identity = {"revision": REVISION, "helper_sha256": helper_sha}
    prepare_inputs(download, extracted, prepared, preparation_identity)
    data_root = Path(read_json(prepared / "data-root.json")["data_root"])
    manifest = prepared / "manifest.unqualified.jsonl"
    profile = Path(tempfile.mkdtemp(prefix="workers-", dir=work_root))
    engine = LibreOfficePool(profile, workers=1, timeout=120)
    try:
        engine.probe()
        emit("engine_ready", version=engine.version)
        accepted, diagnostics = qualify_batches(
            manifest, data_root, prepared / "qualification-batches", engine, batch_size, helper_sha
        )
        identity = {
            "source_manifest_sha256": digest(manifest),
            "engine": engine.version,
            "dataset_revision": REVISION,
            "helper_sha256": helper_sha,
            "data_root": str(data_root),
        }
        result = publish_results(prepared, accepted, diagnostics, identity)
    finally:
        engine.close()
    # Recheck original seed/oracle hashes, qualification flags and nonempty catalog.
    catalog = Catalog(result / "manifest.qualified.jsonl", data_root)
    emit(
        "SP06_DATASET_READY",
        qualified=len(catalog.tasks),
        total=len(diagnostics),
        data_root=str(data_root),
        results=str(result),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True, help="Node-local storage for LO profiles and scratch")
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 1000:
        parser.error("batch-size must be between 1 and 1000")

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    run(args.download_root, args.work_root, args.batch_size)


if __name__ == "__main__":
    main()
