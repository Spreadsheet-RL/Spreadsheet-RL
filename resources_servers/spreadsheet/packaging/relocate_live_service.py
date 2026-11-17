"""Move only dataset paths, gracefully restart the exact idle service replica.

No key rotation, package installation, state deletion or SIGKILL. A failed new
startup retains the new config and PID receipt for inspection/retry; the old
config is backed up but cannot make missing old dataset paths usable again.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import sqlite3
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def moved_config(original, data_root, manifest):
    updated = dict(original)
    for field, target in (("data_root", data_root), ("manifest", manifest)):
        old = str(original[field])
        expected = old.replace("/nlp/tungks/", "/nlp/users/tungks/", 1)
        if old == expected or str(target.resolve()) != expected:
            raise ValueError("Only the verified tungks -> users/tungks dataset move is supported")
        updated[field] = expected
    return updated


def replace_config(path, body):
    info = path.stat()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(body)
            stream.flush()
            os.fchmod(stream.fileno(), info.st_mode & 0o777)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def idle(state_root):
    database = state_root / "sessions/state.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        pending = connection.execute("SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
    if pending:
        raise ValueError("Service still has queued/running jobs; no process was stopped")


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "launcher",
        "bundle",
        "data-root",
        "manifest",
        "qualification-report",
        "python-rootfs-receipt",
        "service-root",
        "backup-dir",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--expected-pid", type=int, required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong service node")
    config = args.service_root / "private/service.json"
    if config.is_symlink() or not config.is_file():
        parser.error("Require the existing regular service config")
    original_body = config.read_bytes()
    original = json.loads(original_body)
    updated = moved_config(original, args.data_root, args.manifest)
    if original.get("allow_unqualified_tasks", False) or original.get("python_isolation") != "chroot_uid_seccomp":
        parser.error("Require the qualified isolated service")
    spec = importlib.util.spec_from_file_location("verified_service_launcher", args.launcher)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    receipt = json.loads((args.service_root / "service-process.json").read_text())
    pid = receipt.get("pid")
    if pid != args.expected_pid or not launcher.owned_process(pid, config):
        parser.error("PID does not match the exact expected service command")
    if receipt.get("config_sha256") != hashlib.sha256(original_body).hexdigest():
        parser.error("Config differs from the running service launch receipt")
    if digest(args.manifest) != receipt.get("manifest_sha256"):
        parser.error("Relocated manifest differs from the running qualified catalog")
    # All data/engine/config checks happen before stopping the live process.
    report = json.loads(args.qualification_report.read_text())
    rows = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    if (
        not rows
        or len(rows) != report["qualified"]
        or any(
            not row.get("libreoffice_qualified")
            or row.get("qualified_engine") != report["engine"]
            or row.get("dataset_revision") != report["dataset_revision"]
            for row in rows
        )
    ):
        parser.error("Qualification report and relocated manifest differ")
    rootfs = Path(args.python_rootfs_receipt.read_text().strip()).resolve()
    if str(rootfs) != original.get("python_rootfs") or not (rootfs / "usr/bin/python3.12").is_file():
        parser.error("Qualified node-local Python rootfs is missing or changed")
    expected = dict(updated)
    expected.update(
        host="0.0.0.0",
        port=int(args.service_url.rsplit(":", 1)[1]),
        state_root=str(args.service_root / "state"),
        api_key_file=str(args.service_root / "private/api-key"),
    )
    if expected != updated:
        parser.error("Service identity/state/address differs from the existing configuration")
    key = (args.service_root / "private/api-key").read_text().strip()
    launcher.validate_adapter_config(args.service_root / "private/gym.private.yaml", args.service_url, key)
    del key
    python = args.bundle / "service-venv/bin/python"
    code = """
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from resources_servers.spreadsheet.service.catalog import Catalog
catalog=Catalog(Path(sys.argv[2]),Path(sys.argv[3]))
print(json.dumps({'event':'RELOCATED_CATALOG_VALIDATED','tasks':len(catalog.tasks)}),flush=True)
"""
    emit("SERVICE_RELOCATION_PREFLIGHT", old_pid=pid, data_root=str(args.data_root), manifest=str(args.manifest))
    subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-u",
            "-c",
            code,
            str(args.bundle / "source"),
            str(args.manifest),
            str(args.data_root),
        ],
        check=True,
        timeout=600,
    )
    idle(Path(original["state_root"]))
    args.backup_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    backup = args.backup_dir / "service.json.before"
    backup.write_bytes(original_body)
    backup.chmod(0o600)
    if backup.read_bytes() != original_body:
        raise ValueError("Backup verification failed")
    new_body = (json.dumps(updated, indent=2) + "\n").encode()
    record = dict(
        hostname=args.expected_hostname,
        old_pid=pid,
        backup=str(backup),
        config=str(config),
        before_sha256=hashlib.sha256(original_body).hexdigest(),
        after_sha256=hashlib.sha256(new_body).hexdigest(),
        status="prepared",
    )
    record_path = args.backup_dir / "relocation.json"

    def save():
        record_path.write_text(json.dumps(record, indent=2) + "\n")
        record_path.chmod(0o600)

    save()
    credentials = {
        path: digest(path)
        for path in (args.service_root / "private/api-key", args.service_root / "private/gym.private.yaml")
    }
    fd = os.pidfd_open(pid)
    try:
        if not launcher.owned_process(pid, config) or config.read_bytes() != original_body:
            raise ValueError("Service identity/config changed during preflight")
        idle(Path(original["state_root"]))
        emit("SERVICE_GRACEFUL_STOP", pid=pid)
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], 60)[0]:
            record["status"] = "stop_timeout_config_unchanged"
            save()
            raise TimeoutError("Service did not stop gracefully; no SIGKILL or config change")
    finally:
        os.close(fd)
    if config.read_bytes() != original_body:
        raise ValueError("Config changed while waiting for shutdown")
    replace_config(config, new_body)
    record["status"] = "config_relocated_restart_pending"
    save()
    command = [str(python), "-B", "-u", str(args.launcher)]
    for name in ("bundle", "data_root", "manifest", "qualification_report", "python_rootfs_receipt", "service_root"):
        command.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
    command.extend(["--service-url", args.service_url])
    env = {
        **os.environ,
        "PYTHONPATH": str(args.bundle / "source"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    subprocess.run(command, check=True, env=env, timeout=660)
    if any(digest(path) != before for path, before in credentials.items()):
        raise RuntimeError("Credential files changed unexpectedly")
    record["status"] = "restarted"
    save()
    emit("SERVICE_DATA_PATHS_RELOCATED", receipt=str(record_path), credentials_unchanged=True)


if __name__ == "__main__":
    main()
