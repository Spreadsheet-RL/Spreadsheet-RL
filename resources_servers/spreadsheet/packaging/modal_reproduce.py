"""Test the locked runtime twice on clean CPU containers; no HF publication.

Use the existing Modal CLI and an explicitly authorized profile. Large artifacts
stay on Modal. The local entrypoint saves only logs and small JSON receipts.
"""

import hashlib
import json
from pathlib import Path

import modal


HERE = Path(__file__).resolve().parent if modal.is_local() else Path("/")
LOCK_PATH = HERE / "runtime.lock.json"
LOCK = json.loads(LOCK_PATH.read_text())
app = modal.App("spreadsheet-runtime-reproducibility")
volume = modal.Volume.from_name("spreadsheet-gym-runtime-builds", create_if_missing=False)
image = (
    modal.Image.from_registry(LOCK["base_image"], add_python="3.12")
    .add_local_file(LOCK_PATH, "/runtime.lock.json", copy=True)
    .add_local_file(HERE / "verify_runtime.py", "/verify_runtime.py", copy=True)
)


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@app.function(image=image, volumes={"/artifacts": volume}, cpu=1, memory=2048, timeout=300)
def fetch_artifact():
    import os
    import ssl
    import urllib.request
    from uuid import uuid4

    import certifi

    artifact = LOCK["artifact"]
    target = Path("/artifacts/reproduction-inputs") / artifact["sha256"] / "runtime.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != artifact["bytes"] or file_sha(target) != artifact["sha256"]:
            raise RuntimeError("Existing immutable artifact cache differs; preserved for inspection")
    else:
        partial = target.with_name(uuid4().hex + ".partial")
        url = f"https://huggingface.co/datasets/{artifact['repository']}/resolve/{artifact['revision']}/{artifact['filename']}"
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, timeout=120, context=context) as response, partial.open("xb") as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
        if partial.stat().st_size != artifact["bytes"] or file_sha(partial) != artifact["sha256"]:
            raise RuntimeError("Public artifact checksum/size mismatch")
        os.replace(partial, target)
        volume.commit()
    return {"path": str(target), "sha256": artifact["sha256"], "bytes": artifact["bytes"]}


@app.function(
    image=image,
    volumes={"/artifacts": volume},
    cpu=2,
    memory=8192,
    timeout=900,
    block_network=True,
    single_use_containers=True,
    max_containers=2,
)
def verify_one(label: str, run_id: str):
    import shutil
    import socket
    import subprocess
    import sys

    archive = Path("/artifacts/reproduction-inputs") / LOCK["artifact"]["sha256"] / "runtime.tar.gz"
    work = Path("/work") / label
    completed = subprocess.run(
        [
            sys.executable,
            "/verify_runtime.py",
            "install",
            "--archive",
            str(archive),
            "--lock",
            "/runtime.lock.json",
            "--work",
            str(work),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=840,
    )
    destination = Path("/artifacts/reproduction-results") / run_id / label
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "worker.log").write_text(completed.stdout)
    for path in work.glob("*"):
        if path.is_file() and path.suffix in {".log", ".xml", ".json"}:
            shutil.copyfile(path, destination / path.name)
    volume.commit()
    if not (work / "report.json").is_file():
        raise RuntimeError("Worker produced no report; inspect persisted worker.log")
    result = json.loads((work / "report.json").read_text())
    result.update(
        label=label,
        exit_code=completed.returncode,
        evidence_volume_path=str(destination),
        modal_hostname=socket.gethostname(),
    )
    print(
        "RUNTIME_REPRO_WORKER="
        + json.dumps(
            {key: result.get(key) for key in ("label", "checks_passed", "phase", "error", "tests", "elapsed_seconds")}
        ),
        flush=True,
    )
    return result


@app.local_entrypoint()
def main(output_dir: str, source_commit: str):
    import subprocess
    from uuid import uuid4

    root = Path(__file__).resolve().parents[3]
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() != source_commit:
        raise RuntimeError("Source commit differs")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True):
        raise RuntimeError("Use a clean committed source checkout")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    run_id = "reproduce-" + uuid4().hex
    fetched = fetch_artifact.remote()
    futures = [verify_one.spawn(label, run_id) for label in ("a", "b")]
    results = [future.get() for future in futures]
    successful = all(result["checks_passed"] and result["exit_code"] == 0 for result in results)
    distinct = results[0]["modal_hostname"] != results[1]["modal_hostname"]
    fields = ("runtime_fingerprint_sha256", "rebuilt_root_sha256", "behavior_sha256")
    comparisons = {
        name: all(name in result for result in results) and results[0].get(name) == results[1].get(name)
        for name in fields
    }
    report = {
        "source_commit": source_commit,
        "lock": LOCK,
        "modal_image_id": image.object_id,
        "run_id": run_id,
        "artifact": fetched,
        "independent_containers": distinct,
        "equal_fingerprints": comparisons,
        "workers": results,
        "checks_passed": successful and distinct and all(comparisons.values()),
        "scope": "Offline redeployment and sandbox reconstruction from pinned artifact; not a byte-identical fresh apt/PyPI rebuild",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "RUNTIME_REPRO_RESULT="
        + json.dumps(
            {key: report[key] for key in ("checks_passed", "run_id", "independent_containers", "equal_fingerprints")}
        ),
        flush=True,
    )
    if not report["checks_passed"]:
        raise RuntimeError("Runtime reproduction checks failed; inspect report.json")
