"""Start the bundled API on an existing Linux node; reuse a matching live process.

No downloads, containers, credential output, or automatic process termination.
Service state and Python root must reside on node-local storage.
"""

import argparse
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


MODULE = "resources_servers.spreadsheet.service.app"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def owned_process(pid, config, proc_root=Path("/proc")):
    if type(pid) is not int or pid <= 1:
        return False
    try:
        argv = (proc_root / str(pid) / "cmdline").read_bytes().decode().split("\0")
    except (FileNotFoundError, ProcessLookupError):
        return False
    expected = ["-m", MODULE, "--config", str(config)]
    return any(argv[index : index + len(expected)] == expected for index in range(len(argv)))


def validate_config(config, expected):
    actual = json.loads(config.read_text())
    if any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError("Existing configuration differs from the requested paths/port; refusing to replace it")
    if actual.get("python_isolation") != "chroot_uid_seccomp" or actual.get("allow_unqualified_tasks", False):
        raise ValueError("Configuration must require isolated Python and qualified tasks")
    return actual


def validate_adapter_config(path, base, key):
    # init_service writes these fields as JSON-quoted YAML scalars. Do not need
    # a YAML dependency in the standalone service to validate its own output.
    values = {}
    for line in path.read_text().splitlines():
        name, separator, value = line.strip().partition(":")
        if separator and name in ("service_url", "api_key", "session_secret_key"):
            if name in values:
                raise ValueError("Duplicate adapter credential/address fields")
            values[name] = json.loads(value)
    if (
        values.get("service_url") != base
        or values.get("api_key") != key
        or not isinstance(values.get("session_secret_key"), str)
        or len(values["session_secret_key"]) < 32
    ):
        raise ValueError(
            "Existing adapter config has a different address/key or invalid session key; refusing to replace it"
        )


def probe(opener, base, key, expected_engine, expected_tools):
    def get(path, authenticated=False):
        headers = {"Authorization": "Bearer " + key} if authenticated else {}
        request = urllib.request.Request(base + path, headers=headers)
        with opener.open(request, timeout=3) as response:
            return json.load(response)

    health = get("/health")
    if (
        health.get("status") != "ready"
        or health.get("python_isolation") != "chroot_uid_seccomp"
        or health.get("engine") != expected_engine
    ):
        raise ValueError("Health response does not match the qualified runtime")
    tools = get("/tools", True)["tools"]
    if len(tools) != len(expected_tools) or set(tools) != set(expected_tools):
        raise ValueError("Service tool list differs from the bundled schemas")
    try:
        get("/tools")
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
    else:
        raise ValueError("Unauthenticated tool access was accepted")
    return health, tools


def start(args):
    from resources_servers.spreadsheet.schemas import TOOL_MODELS

    bundle, root = args.bundle.resolve(), args.service_root.resolve()
    data, manifest = args.data_root.resolve(), args.manifest.resolve()
    rootfs = Path(args.python_rootfs_receipt.read_text().strip()).resolve()
    if not (rootfs / "usr/bin/python3.12").is_file():
        raise ValueError("The probed Python root filesystem is missing")
    report = json.loads(args.qualification_report.read_text())
    if not manifest.is_file() or not report["qualified"]:
        raise ValueError("A nonempty qualified catalog is required")
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    if len(rows) != report["qualified"] or any(
        not row.get("libreoffice_qualified")
        or row.get("qualified_engine") != report["engine"]
        or row.get("dataset_revision") != report["dataset_revision"]
        for row in rows
    ):
        raise ValueError("Qualified catalog and compatibility report differ")
    manifest_sha = digest(manifest)
    advertised = urlsplit(args.service_url)
    if (
        advertised.scheme != "http"
        or not advertised.hostname
        or not advertised.port
        or advertised.username
        or advertised.password
        or advertised.path not in ("", "/")
        or advertised.query
        or advertised.fragment
    ):
        raise ValueError("service-url must be http://LOCAL_SERVICE_IP:PORT")
    base = f"http://{advertised.hostname}:{advertised.port}"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private, state = root / "private", root / "state"
    private.mkdir(exist_ok=True, mode=0o700)
    config = private / "service.json"
    env = dict(os.environ, PYTHONPATH=str(bundle / "source"), PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1")
    env.pop("PYTHONHOME", None)
    python = str(bundle / "service-venv/bin/python")
    expected = {
        "host": "0.0.0.0",
        "port": advertised.port,
        "data_root": str(data),
        "manifest": str(manifest),
        "state_root": str(state),
        "python_rootfs": str(rootfs),
        "api_key_file": str(private / "api-key"),
    }
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        files = [private / name for name in ("api-key", "service.json", "gym.private.yaml")]
        if any(path.exists() for path in files):
            if not all(path.is_file() for path in files):
                raise ValueError("Partial private configuration exists; refusing to replace keys")
        else:
            subprocess.run(
                [
                    python,
                    "-m",
                    "resources_servers.spreadsheet.init_service",
                    "--data-root",
                    str(data),
                    "--manifest",
                    str(manifest),
                    "--state-root",
                    str(state),
                    "--python-rootfs",
                    str(rootfs),
                    "--config-dir",
                    str(private),
                    "--service-url",
                    base,
                    "--port",
                    str(advertised.port),
                ],
                env=env,
                cwd=bundle / "source",
                check=True,
            )
        validate_config(config, expected)
        if any(path.stat().st_mode & 0o077 for path in files):
            raise ValueError("Private configuration files must not grant group/other permissions")
        key = (private / "api-key").read_text().strip()
        validate_adapter_config(private / "gym.private.yaml", base, key)
        config_sha = digest(config)
        pid_file, log = root / "service-process.json", root / "service.log"
        receipt = json.loads(pid_file.read_text()) if pid_file.exists() else {}
        pid = receipt.get("pid")
        reused = bool(pid and owned_process(pid, config))
        if reused and (receipt.get("config_sha256") != config_sha or receipt.get("manifest_sha256") != manifest_sha):
            raise ValueError("A running service uses a previous configuration/catalog; no automatic restart")
        process = None
        if not reused:
            # Check the advertised address belongs to this node and the port is free.
            with socket.socket() as address_probe:
                address_probe.bind((advertised.hostname, 0))
            with socket.socket() as port_probe:
                port_probe.bind(("0.0.0.0", advertised.port))
            with log.open("ab", buffering=0) as stream:
                process = subprocess.Popen(
                    [
                        python,
                        "-u",
                        "-m",
                        MODULE,
                        "--config",
                        str(config),
                    ],
                    cwd=bundle / "source",
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            pid = process.pid
            write_json(
                pid_file,
                {
                    "pid": pid,
                    "config_sha256": config_sha,
                    "manifest_sha256": manifest_sha,
                    "config": str(config),
                },
            )
        emit("SP08_SERVICE_PROCESS", pid=pid, reused=reused, log=str(log))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline, next_progress = time.monotonic() + args.startup_timeout, 0
        while True:
            if (process is not None and process.poll() is not None) or not owned_process(pid, config):
                tail = log.read_text(errors="replace")[-4000:] if log.exists() else ""
                raise RuntimeError("Service exited during startup. Log tail:\n" + tail)
            try:
                health, tools = probe(opener, base, key, report["engine"], TOOL_MODELS)
                break
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Startup deadline reached; inspect {log}. PID receipt retained for retry."
                    ) from None
                if time.monotonic() >= next_progress:
                    emit("SP08_WAITING_FOR_STARTUP", pid=pid, log=str(log))
                    next_progress = time.monotonic() + 15
                time.sleep(1)
        ready = {
            "pid": pid,
            "service_url": base,
            "health": health,
            "tool_count": len(tools),
            "manifest_sha256": manifest_sha,
            "qualified_tasks": report["qualified"],
            "gym_private_config": str(private / "gym.private.yaml"),
            "log": str(log),
        }
        write_json(root / "readiness-SP08.json", ready)
        emit("SP08_SERVICE_READY", **ready)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "data-root", "manifest", "qualification-report", "python-rootfs-receipt", "service-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--service-url", required=True)
    parser.add_argument("--startup-timeout", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.startup_timeout <= 1800:
        parser.error("startup-timeout must be 1–1800 seconds")
    start(args)


if __name__ == "__main__":
    main()
