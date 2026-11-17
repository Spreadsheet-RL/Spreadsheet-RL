"""Eight-worker CPU audit using the locked offline runtime and public dataset.

Persisted batches are private: diagnostics include oracle cells and formulas.
No HF publication, company-node modification, or training launch is performed.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import modal


HERE = (
    Path(__file__).resolve().parent
    if modal.is_local()
    else Path("/source/resources_servers/spreadsheet/packaging")
)
SOURCE = HERE.parents[2]
sys.path.insert(0, str(SOURCE))
LOCK = json.loads((HERE / "runtime.lock.json").read_text())
VOLUME = modal.Volume.from_name(
    "spreadsheet-gym-runtime-builds", create_if_missing=False
)
app = modal.App("spreadsheet-rejected-task-audit")
BUNDLE = "/opt/runtime/spreadsheet-gym-ubuntu24.04-x86_64"
PYTHON = BUNDLE + "/service-venv/bin/python"
SCRIPT = "/source/resources_servers/spreadsheet/packaging/audit_rejections.py"


def bootstrap_runtime(artifact):
    import ssl
    import subprocess
    import tarfile
    import tempfile
    import urllib.request

    import certifi

    cached = Path("/inputs/reproduction-inputs") / artifact["sha256"] / "runtime.tar.gz"
    with tempfile.TemporaryDirectory() as temporary:
        archive = cached
        if not archive.exists():
            archive = Path(temporary) / "runtime.tar.gz"
            url = f"https://huggingface.co/datasets/{artifact['repository']}/resolve/{artifact['revision']}/{artifact['filename']}"
            with (
                urllib.request.urlopen(
                    url,
                    context=ssl.create_default_context(cafile=certifi.where()),
                    timeout=120,
                ) as response,
                archive.open("xb") as target,
            ):
                while block := response.read(1024 * 1024):
                    target.write(block)
        with archive.open("rb") as stream:
            if (
                hashlib.file_digest(stream, "sha256").hexdigest() != artifact["sha256"]
                or archive.stat().st_size != artifact["bytes"]
            ):
                raise ValueError("Locked runtime artifact checksum differs")
        with tarfile.open(archive) as tar:
            tar.extractall("/opt/runtime", filter="data")
    installed = subprocess.run(
        ["bash", BUNDLE + "/install_offline.sh"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if installed.returncode:
        print(installed.stdout[-8000:] + installed.stderr[-4000:])
        raise RuntimeError("Offline runtime installation failed")
    subprocess.run(
        [
            PYTHON,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            BUNDLE + "/wheelhouse",
            "pyarrow==22.0.0",
            "pytest==9.0.3",
            "httpx==0.28.1",
        ],
        check=True,
        timeout=180,
    )
    print("AUDIT_LOCKED_RUNTIME_READY", flush=True)


image = (
    modal.Image.from_registry(LOCK["base_image"], add_python="3.12")
    .add_local_file(HERE / "runtime.lock.json", "/source/resources_servers/spreadsheet/packaging/runtime.lock.json", copy=True)
    .run_function(
        bootstrap_runtime,
        args=(LOCK["artifact"],),
        volumes={"/inputs": VOLUME.read_only()},
        cpu=2,
        memory=8192,
        timeout=900,
    )
    .add_local_dir(
        SOURCE / "resources_servers/spreadsheet",
        "/source/resources_servers/spreadsheet",
        ignore=["__pycache__", "*.pyc"],
        copy=True,
    )
)


def subprocess_run(arguments, log, *, timeout=1800):
    import subprocess

    with log.open("w") as stream:
        result = subprocess.run(
            arguments,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": "/source",
            },
        )
    if result.returncode:
        print(log.read_text()[-10000:], flush=True)
        raise RuntimeError(
            f"Audit subprocess exit {result.returncode}; inspect {log.name}"
        )


@app.function(
    image=image, volumes={"/artifacts": VOLUME}, cpu=2, memory=8192, timeout=900
)
def prepare_inputs(run_id, source_commit, limit, batch_size):
    import ssl
    import time
    import urllib.request

    import certifi
    from resources_servers.spreadsheet.packaging.prepare_dataset import FILES, REVISION

    data = Path("/artifacts/audit-inputs") / REVISION
    data.mkdir(parents=True, exist_ok=True)
    for name, (size, expected) in FILES.items():
        path = data / name
        if not path.exists():
            url = f"https://huggingface.co/datasets/Spreadsheet-RL/Spreadsheet-RL/resolve/{REVISION}/{name}"
            partial = path.with_suffix(path.suffix + ".partial")
            for attempt in range(3):
                try:
                    with (
                        urllib.request.urlopen(
                            url,
                            context=ssl.create_default_context(cafile=certifi.where()),
                            timeout=180,
                        ) as response,
                        partial.open("wb") as target,
                    ):
                        while block := response.read(1024 * 1024):
                            target.write(block)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
            with partial.open("rb") as stream:
                if (
                    partial.stat().st_size != size
                    or hashlib.file_digest(stream, "sha256").hexdigest() != expected
                ):
                    raise ValueError("Dataset download checksum differs")
            partial.replace(path)
        with path.open("rb") as stream:
            if (
                path.stat().st_size != size
                or hashlib.file_digest(stream, "sha256").hexdigest() != expected
            ):
                raise ValueError("Cached dataset differs from pinned input")
    run = Path("/artifacts/audit-runs") / run_id
    run.mkdir(parents=True, exist_ok=True)
    if not (run / "tasks.jsonl").exists():
        subprocess_run(
            [
                PYTHON,
                "-I",
                SCRIPT,
                "prepare",
                "--data",
                str(data),
                "--output",
                str(run / "tasks.jsonl"),
            ],
            run / "prepare.log",
        )
    tasks = min(limit, 8647) if limit else 8647
    identity = {
        "run_id": run_id,
        "source_commit": source_commit,
        "dataset_revision": REVISION,
        "runtime_artifact": LOCK["artifact"],
        "tasks": tasks,
        "batch_size": batch_size,
        "task_order_sha256": hashlib.sha256(
            (run / "tasks.jsonl").read_bytes()
        ).hexdigest(),
    }
    identity_path = run / "identity.json"
    if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
        raise ValueError("Existing run belongs to a different audit contract")
    identity_path.write_text(json.dumps(identity, indent=2) + "\n")
    # These tests execute in the same locked Linux runtime used by all workers.
    subprocess_run(
        [
            PYTHON,
            "-m",
            "pytest",
            "-c",
            "/dev/null",
            "-q",
            "/source/resources_servers/spreadsheet/tests/test_reward_readers.py",
            "/source/resources_servers/spreadsheet/tests/test_workbook.py",
            "/source/resources_servers/spreadsheet/tests/test_rejection_audit.py",
        ],
        run / "regression-tests.log",
        timeout=240,
    )
    VOLUME.commit()
    return identity


@app.function(
    image=image,
    volumes={"/artifacts": VOLUME},
    cpu=2,
    memory=8192,
    timeout=2400,
    max_containers=8,
    block_network=True,
)
def audit_batch(run_id, source_commit, start, size):
    from resources_servers.spreadsheet.packaging.prepare_dataset import REVISION

    run = Path("/artifacts/audit-runs") / run_id
    destination = run / "batches" / f"{start:05d}"
    destination.mkdir(parents=True, exist_ok=True)
    try:
        subprocess_run(
            [
                PYTHON,
                "-I",
                SCRIPT,
                "batch",
                "--data",
                str(Path("/artifacts/audit-inputs") / REVISION),
                "--bundle",
                BUNDLE,
                "--tasks",
                str(run / "tasks.jsonl"),
                "--output",
                str(destination),
                "--start",
                str(start),
                "--size",
                str(size),
                "--source-commit",
                source_commit,
            ],
            destination / "worker.log",
            timeout=2300,
        )
        result = json.loads((destination / "complete.json").read_text())
        receipt = {
            "start": start,
            "counts": result["counts"],
            "task_id": os.environ.get("MODAL_TASK_ID"),
        }
        print("AUDIT_BATCH=" + json.dumps(receipt), flush=True)
        return receipt
    finally:
        VOLUME.commit()


@app.function(
    image=image, volumes={"/artifacts": VOLUME}, cpu=2, memory=8192, timeout=600
)
def summarize(run_id):
    from resources_servers.spreadsheet.packaging.prepare_dataset import REVISION

    run = Path("/artifacts/audit-runs") / run_id
    subprocess_run(
        [
            PYTHON,
            "-I",
            SCRIPT,
            "summarize",
            "--data",
            str(Path("/artifacts/audit-inputs") / REVISION),
            "--output",
            str(run),
        ],
        run / "summary.log",
        timeout=500,
    )
    VOLUME.commit()
    return json.loads((run / "summary.json").read_text())


@app.local_entrypoint()
def main(
    source_commit: str,
    output_dir: str,
    run_id: str = "",
    limit: int = 0,
    batch_size: int = 50,
):
    import re
    import subprocess
    from uuid import uuid4

    if subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True
    ).strip() != source_commit or subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=SOURCE, text=True
    ):
        raise ValueError("Audit requires the exact clean committed source")
    if limit < 0 or not 1 <= batch_size <= 100:
        raise ValueError("Invalid limit/batch size")
    run_id = run_id or "audit-" + source_commit[:12] + "-" + uuid4().hex[:8]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id):
        raise ValueError("Unsafe run ID")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = {
        "run_id": run_id,
        "source_commit": source_commit,
        "app_id": app.app_id,
        "workers": 8,
    }
    (output / "run-started.json").write_text(json.dumps(started, indent=2) + "\n")
    identity = prepare_inputs.remote(run_id, source_commit, limit, batch_size)
    inputs = [
        (run_id, source_commit, start, min(batch_size, identity["tasks"] - start))
        for start in range(0, identity["tasks"], batch_size)
    ]
    receipts = []
    for receipt in audit_batch.starmap(inputs, order_outputs=False):
        receipts.append(receipt)
        print(
            "AUDIT_PROGRESS="
            + json.dumps(
                {
                    "batches_done": len(receipts),
                    "batches_total": len(inputs),
                    "last": receipt["start"],
                }
            ),
            flush=True,
        )
        (output / "progress.json").write_text(json.dumps(receipts, indent=2) + "\n")
    result = summarize.remote(run_id)
    result["modal_app_id"] = app.app_id
    result["worker_task_ids"] = sorted({r["task_id"] for r in receipts if r["task_id"]})
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    for name in (
        "rejected.audit.jsonl",
        "recovered.manifest.jsonl",
        "regression-tests.log",
    ):
        content = b"".join(VOLUME.read_file(f"audit-runs/{run_id}/{name}"))
        (output / name).write_bytes(content)
    print(
        "AUDIT_RESULT="
        + json.dumps(
            {
                k: result[k]
                for k in (
                    "checked",
                    "baseline_qualified",
                    "baseline_rejected",
                    "verdicts",
                    "recovered_by_split",
                )
            }
        ),
        flush=True,
    )
