"""Test the locked runtime twice on clean CPU containers; optionally publish a repair.

Use the existing Modal CLI and an explicitly authorized profile. Large artifacts
stay on Modal. The local entrypoint saves only logs and small JSON receipts.
"""

import hashlib
import json
import os
from pathlib import Path

import modal


HERE = Path(__file__).resolve().parent if modal.is_local() else Path("/")
LOCK_NAME = os.environ.get("SPREADSHEET_RUNTIME_LOCK", "runtime.lock.json")
if Path(LOCK_NAME).name != LOCK_NAME:
    raise ValueError("Select a lock filename inside the packaging directory")
LOCK_PATH = HERE / LOCK_NAME if modal.is_local() else Path("/runtime.lock.json")
LOCK = json.loads(LOCK_PATH.read_text())
app = modal.App("spreadsheet-runtime-reproducibility")
volume = modal.Volume.from_name(
    "spreadsheet-gym-runtime-builds", create_if_missing=False
)
image = (
    modal.Image.from_registry(LOCK["base_image"], add_python="3.12")
    .add_local_file(LOCK_PATH, "/runtime.lock.json", copy=True)
    .add_local_file(HERE / "verify_runtime.py", "/verify_runtime.py", copy=True)
)
publisher_image = (
    image.pip_install("huggingface_hub==0.36.0")
    .run_commands(
        'python -c "import certifi; from pathlib import Path; '
        "p=Path('/etc/ssl/certs/ca-certificates.crt'); "
        'p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(Path(certifi.where()).read_bytes())"'
    )
    .env({"SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"})
)


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@app.function(
    image=image, volumes={"/artifacts": volume}, cpu=1, memory=2048, timeout=300
)
def fetch_artifact():
    import os
    import ssl
    import urllib.request
    from uuid import uuid4

    import certifi

    artifact = LOCK["artifact"]
    target = (
        Path("/artifacts/reproduction-inputs") / artifact["sha256"] / "runtime.tar.gz"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if (
            target.stat().st_size != artifact["bytes"]
            or file_sha(target) != artifact["sha256"]
        ):
            raise RuntimeError(
                "Existing immutable artifact cache differs; preserved for inspection"
            )
    else:
        partial = target.with_name(uuid4().hex + ".partial")
        url = f"https://huggingface.co/datasets/{artifact['repository']}/resolve/{artifact['revision']}/{artifact['filename']}"
        context = ssl.create_default_context(cafile=certifi.where())
        with (
            urllib.request.urlopen(url, timeout=120, context=context) as response,
            partial.open("xb") as stream,
        ):
            while block := response.read(1024 * 1024):
                stream.write(block)
        if (
            partial.stat().st_size != artifact["bytes"]
            or file_sha(partial) != artifact["sha256"]
        ):
            raise RuntimeError("Public artifact checksum/size mismatch")
        os.replace(partial, target)
        volume.commit()
    return {
        "path": str(target),
        "sha256": artifact["sha256"],
        "bytes": artifact["bytes"],
    }


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
def verify_one(label: str, run_id: str, source_commit: str, repair: bool):
    import shutil
    import socket
    import subprocess
    import sys

    archive = (
        Path("/artifacts/reproduction-inputs")
        / LOCK["artifact"]["sha256"]
        / "runtime.tar.gz"
    )
    work = Path("/work") / label
    command = [
        sys.executable,
        "/verify_runtime.py",
        "install",
        "--archive",
        str(archive),
        "--lock",
        "/runtime.lock.json",
        "--work",
        str(work),
    ]
    if repair:
        command.extend(["--repair-commit", source_commit])
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=840,
    )
    destination = Path("/artifacts/reproduction-results") / run_id / label
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "worker.log").write_text(completed.stdout)
    for path in work.glob("*"):
        if (
            path.is_file()
            and path.name != "report.json"
            and (
                path.suffix in {".log", ".xml", ".json"}
                or path.name == "runtime-repaired.tar.gz"
            )
        ):
            shutil.copyfile(path, destination / path.name)
    if not (work / "report.json").is_file():
        raise RuntimeError("Worker produced no report; inspect persisted worker.log")
    result = json.loads((work / "report.json").read_text())
    result.update(
        label=label,
        exit_code=completed.returncode,
        evidence_volume_path=str(destination),
        modal_hostname=socket.gethostname(),
        modal_task_id=os.environ.get("MODAL_TASK_ID"),
        function_call_id=modal.current_function_call_id(),
        worker_source_commit=source_commit,
    )
    (destination / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    volume.commit()
    print(
        "RUNTIME_REPRO_WORKER="
        + json.dumps(
            {
                key: result.get(key)
                for key in (
                    "label",
                    "checks_passed",
                    "phase",
                    "error",
                    "tests",
                    "elapsed_seconds",
                )
            }
        ),
        flush=True,
    )
    # A large return would use an external blob store, blocked in this worker.
    return {
        "label": label,
        "report": str(destination.relative_to("/artifacts") / "report.json"),
    }


@app.function(
    image=publisher_image,
    volumes={"/artifacts": volume},
    cpu=1,
    memory=2048,
    timeout=600,
)
def publish_repaired(volume_path: str, metadata: dict):
    import tempfile

    from huggingface_hub import CommitOperationAdd, HfApi

    archive = Path("/artifacts") / volume_path
    assert file_sha(archive) == metadata["archive"]["sha256"]
    repository = LOCK["artifact"]["repository"]
    filename = "spreadsheet-gym-ubuntu24.04-x86_64-repro1.tar.gz"
    api = HfApi(token=os.environ["HF_TOKEN"])
    parent = api.repo_info(repository, repo_type="dataset").sha
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "SHA256SUMS-repro1").write_text(
            metadata["archive"]["sha256"] + "  " + filename + "\n"
        )
        (root / "RUNTIME_REPRODUCTION.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )
        result = api.create_commit(
            repo_id=repository,
            repo_type="dataset",
            parent_commit=parent,
            commit_message="Publish offline-reproduced runtime with aligned glibc sandbox",
            operations=[
                CommitOperationAdd(path_in_repo=filename, path_or_fileobj=str(archive)),
                CommitOperationAdd(
                    path_in_repo="SHA256SUMS-repro1",
                    path_or_fileobj=str(root / "SHA256SUMS-repro1"),
                ),
                CommitOperationAdd(
                    path_in_repo="RUNTIME_REPRODUCTION.json",
                    path_or_fileobj=str(root / "RUNTIME_REPRODUCTION.json"),
                ),
            ],
        )
    return {
        "repository": repository,
        "revision": result.oid,
        "filename": filename,
        **metadata["archive"],
    }


def publish_qualified(report, hf_python, output):
    import subprocess

    results = report["workers"]
    if (
        not report["checks_passed"]
        or not report["independent_containers"]
        or not all(report["equal_fingerprints"].values())
        or report["lock"] != LOCK
        or len(results) != 2
        or not all(result.get("repaired_archive") for result in results)
        or results[0]["repaired_archive"] != results[1]["repaired_archive"]
    ):
        raise ValueError(
            "Publication requires matching qualified repair evidence and lock"
        )
    helper = "from huggingface_hub import HfApi,get_token; import json; t=get_token(); print(json.dumps({'token':t,'owner':HfApi(token=t).whoami()['name']}))"
    credential = json.loads(
        subprocess.check_output([hf_python, "-c", helper], text=True)
    )
    if not credential["token"] or not LOCK["artifact"]["repository"].startswith(
        credential["owner"] + "/"
    ):
        raise ValueError("HF identity does not own the selected runtime repository")
    metadata = {
        "source_commit": report["source_commit"],
        "parent": LOCK["artifact"],
        "archive": results[0]["repaired_archive"],
        "repair": results[0]["repair"],
        "independent_offline_builds": 2,
    }
    archive_path = str(
        Path(results[0]["evidence_volume_path"]).relative_to("/artifacts")
        / "runtime-repaired.tar.gz"
    )
    published = publish_repaired.with_options(
        secrets=[modal.Secret.from_dict({"HF_TOKEN": credential["token"]})]
    ).remote(archive_path, metadata)
    (output / "published.json").write_text(json.dumps(published, indent=2) + "\n")
    print("RUNTIME_REPRO_PUBLISHED=" + json.dumps(published), flush=True)


@app.local_entrypoint()
def resume_publication(report_path: str, hf_python: str):
    """Retry only publication after inspecting a completed qualification report."""
    path = Path(report_path).resolve()
    if (path.parent / "published.json").exists():
        raise ValueError(
            "Publication receipt already exists; inspect it before retrying"
        )
    publish_qualified(json.loads(path.read_text()), hf_python, path.parent)


@app.local_entrypoint()
def main(
    output_dir: str,
    source_commit: str,
    repair: bool = False,
    publish: bool = False,
    hf_python: str = "",
):
    import subprocess
    from uuid import uuid4

    root = Path(__file__).resolve().parents[3]
    if (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        != source_commit
    ):
        raise RuntimeError("Source commit differs")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True):
        raise RuntimeError("Use a clean committed source checkout")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    run_id = "reproduce-" + uuid4().hex
    (output / "run-started.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "source_commit": source_commit,
                "repair": repair,
                "app_id": app.app_id,
            },
            indent=2,
        )
        + "\n"
    )
    fetched = fetch_artifact.remote()
    futures = [
        verify_one.spawn(label, run_id, source_commit, repair) for label in ("a", "b")
    ]
    receipts = [future.get() for future in futures]
    results = []
    for receipt in receipts:
        body = b"".join(volume.read_file(receipt["report"]))
        (output / (receipt["label"] + ".json")).write_bytes(body)
        results.append(json.loads(body))
    successful = all(
        result["checks_passed"] and result["exit_code"] == 0 for result in results
    )
    distinct = (
        bool(results[0]["function_call_id"])
        and results[0]["function_call_id"] != results[1]["function_call_id"]
    )
    fields = ("runtime_fingerprint_sha256", "rebuilt_root_sha256", "behavior_sha256")
    comparisons = {
        name: all(name in result for result in results)
        and results[0].get(name) == results[1].get(name)
        for name in fields
    }
    if repair:
        comparisons["repaired_archive_bytes"] = (
            all(result.get("repaired_archive") for result in results)
            and results[0]["repaired_archive"] == results[1]["repaired_archive"]
        )
    report = {
        "source_commit": source_commit,
        "lock": LOCK,
        "modal_image_id": image.object_id,
        "run_id": run_id,
        "artifact": fetched,
        "independent_containers": distinct,
        "container_contract": "single_use_containers=True; distinct provider function-call IDs",
        "equal_fingerprints": comparisons,
        "workers": results,
        "checks_passed": successful and distinct and all(comparisons.values()),
        "scope": "Offline redeployment and sandbox reconstruction from pinned artifact; not a byte-identical fresh apt/PyPI rebuild",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "RUNTIME_REPRO_RESULT="
        + json.dumps(
            {
                key: report[key]
                for key in (
                    "checks_passed",
                    "run_id",
                    "independent_containers",
                    "equal_fingerprints",
                )
            }
        ),
        flush=True,
    )
    if not report["checks_passed"]:
        raise RuntimeError("Runtime reproduction checks failed; inspect report.json")
    if publish:
        if not repair or not hf_python:
            raise ValueError(
                "Publication requires qualified repair mode and the existing HF-capable Python"
            )
        publish_qualified(report, hf_python, output)
