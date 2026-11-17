"""Apply a pinned three-file service overlay; keep dependencies, state and keys.

Default is read-only preflight. --apply backs up config/receipt, builds a fresh
source directory, gracefully stops the exact idle PID and restarts it. Failed
readiness restores the old config/source automatically, without SIGKILL. Run
directly on the service node when the Bridge write guard excludes /opt.
"""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4


MODULE = "resources_servers.spreadsheet.service.app"
FILES = {name: "resources_servers/spreadsheet/service/" + name for name in ("app.py", "state.py", "workbook.py")}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def atomic(path, body):
    info = path.stat()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), info.st_mode & 0o777)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
    temporary.replace(path)


def private_file(path, body):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != body:
        raise ValueError("Backup verification failed")


def owned(pid, config):
    if type(pid) is not int or pid <= 1:
        return False
    try:
        args = (Path("/proc") / str(pid) / "cmdline").read_bytes().decode().split("\0")
    except OSError:
        return False
    expected = ["-m", MODULE, "--config", str(config)]
    return any(args[i:i + len(expected)] == expected for i in range(len(args)))


def idle(state):
    database = state / "sessions/state.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        pending = db.execute("SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
        opened = db.execute("SELECT count(*) FROM sessions WHERE state='OPEN'").fetchone()[0]
    if pending or opened:
        raise ValueError("Service has open sessions or pending jobs; no process was stopped")


def graceful_stop(pid, config):
    if not owned(pid, config):
        raise ValueError("Service PID identity changed")
    fd = os.pidfd_open(pid)
    try:
        if not owned(pid, config):
            raise ValueError("Service PID identity changed")
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], 60)[0]:
            raise TimeoutError("Service did not stop gracefully; no SIGKILL was sent")
    finally:
        os.close(fd)


def launch(python, source, config, log):
    env = dict(os.environ, PYTHONPATH=str(source), PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONHOME", None)
    with log.open("ab", buffering=0) as stream:
        return subprocess.Popen(
            [python, "-B", "-u", "-m", MODULE, "--config", str(config)],
            cwd=source, env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def ready(process, port, expected_queue=None, timeout=300):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline, progress = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Service exited during startup; inspect its retained log")
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                health = json.load(response)
            if health.get("status") == "ready" and health.get("python_isolation") == "chroot_uid_seccomp":
                if expected_queue is None or health.get("max_pending") == expected_queue:
                    return health
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if time.monotonic() >= progress:
            emit("SERVICE_UPDATE_WAITING", pid=process.pid)
            progress = time.monotonic() + 15
        time.sleep(1)
    raise TimeoutError("Service readiness deadline exceeded")


def validate_overlay(patches, source):
    manifest = json.loads((patches / "PATCH.json").read_text())
    if set(manifest["files"]) != set(FILES) or set(manifest["previous"]) != set(FILES):
        raise ValueError("Only the three reviewed service files may be replaced")
    for name, relative in FILES.items():
        candidate, previous = patches / name, source / relative
        if candidate.is_symlink() or previous.is_symlink():
            raise ValueError("Service source must be regular independent files")
        if digest(candidate) != manifest["files"][name] or digest(previous) != manifest["previous"][name]:
            raise ValueError("Service source hash differs from the reviewed overlay")
        compile(candidate.read_bytes(), str(candidate), "exec")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-root", type=Path, required=True)
    parser.add_argument("--patch-dir", type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong service node")
    root, patches = args.service_root.resolve(), args.patch_dir.resolve()
    config, pid_file = root / "private/service.json", root / "service-process.json"
    before, receipt_body = config.read_bytes(), pid_file.read_bytes()
    original, receipt = json.loads(before), json.loads(receipt_body)
    pid = receipt["pid"]
    if pid != args.expected_pid or not owned(pid, config):
        raise ValueError("Require the exact expected live service PID")
    if receipt["config_sha256"] != digest(config) or receipt["manifest_sha256"] != digest(Path(original["manifest"])):
        raise ValueError("Live config/catalog differs from its launch receipt")
    if original.get("python_isolation") != "chroot_uid_seccomp" or original.get("allow_unqualified_tasks", False):
        raise ValueError("Require isolated Python and qualified tasks")
    proc = Path("/proc") / str(pid)
    argv = proc.joinpath("cmdline").read_bytes().decode().split("\0")
    environment = dict(x.split("=", 1) for x in proc.joinpath("environ").read_bytes().decode().split("\0") if "=" in x)
    source = Path(environment["PYTHONPATH"]).resolve()
    manifest = validate_overlay(patches, source)
    state = Path(original["state_root"])
    idle(state)
    emit("SERVICE_UPDATE_PREFLIGHT_OK", pid=pid, source_commit=manifest["source_commit"], max_pending=4000)
    if not args.apply:
        return

    update = root / ("update-" + manifest["source_commit"][:12] + "-" + uuid4().hex[:8])
    update.mkdir(mode=0o700)
    private_file(update / "service.json.before", before)
    private_file(update / "service-process.json.before", receipt_body)
    new_source = update / "source"
    if any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("Unexpected symlink in dependency bundle source")
    shutil.copytree(source, new_source, ignore=shutil.ignore_patterns("__pycache__"))
    for name, relative in FILES.items():
        (new_source / relative).write_bytes((patches / name).read_bytes())
    changed = dict(original, max_pending=4000)
    new_body = (json.dumps(changed, indent=2) + "\n").encode()
    credentials = {p: digest(p) for p in (Path(original["api_key_file"]), root / "private/gym.private.yaml")}
    record = {"status": "prepared", "source_commit": manifest["source_commit"], "old_pid": pid,
              "old_source": str(source), "new_source": str(new_source), "config": str(config),
              "state_preserved": str(state), "manifest_sha256": receipt["manifest_sha256"]}

    def save():
        target = update / "deployment.json"
        body = (json.dumps(record, indent=2) + "\n").encode()
        if target.exists():
            atomic(target, body)
        else:
            private_file(target, body)

    save()
    if config.read_bytes() != before or pid_file.read_bytes() != receipt_body:
        raise ValueError("Config or launch receipt changed during preparation")
    idle(state)
    graceful_stop(pid, config)
    record["status"] = "old_service_stopped"
    save()
    process = None
    try:
        atomic(config, new_body)
        process = launch(argv[0], new_source, config, update / "service.log")
        health = ready(process, changed["port"], expected_queue=4000)
        if any(digest(p) != expected for p, expected in credentials.items()):
            raise ValueError("Service credentials changed during deployment")
        atomic(pid_file, (json.dumps(dict(receipt, pid=process.pid, config_sha256=digest(config),
            source_root=str(new_source), log=str(update / "service.log")), indent=2) + "\n").encode())
        record.update(status="ready", pid=process.pid, health=health, credentials_unchanged=True)
        save()
        emit("SERVICE_UPDATE_READY", **record, receipt=str(update / "deployment.json"))
    except Exception as error:
        record.update(status="rollback_pending", failure_type=type(error).__name__)
        save()
        if process is not None and process.poll() is None:
            graceful_stop(process.pid, config)
        atomic(config, before)
        restored = launch(argv[0], source, config, update / "rollback.log")
        ready(restored, original["port"])
        atomic(pid_file, (json.dumps(dict(receipt, pid=restored.pid), indent=2) + "\n").encode())
        record.update(status="rolled_back", rollback_pid=restored.pid)
        save()
        emit("SERVICE_UPDATE_ROLLED_BACK", receipt=str(update / "deployment.json"), failure_type=type(error).__name__)
        raise


if __name__ == "__main__":
    main()
