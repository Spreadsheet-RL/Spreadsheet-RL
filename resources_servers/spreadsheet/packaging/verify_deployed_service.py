"""Run Linux reward regressions and the 12-tool API canary after a source update.

No model inference, package installation or modification of training files.
Uses the running service's Python/source/rootfs and fresh node-local QA output.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-root", type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expected_hostname:
        parser.error("Wrong service node")
    root, patches = args.service_root.resolve(), Path(__file__).resolve().parent
    config_path = root / "private/service.json"
    config = json.loads(config_path.read_text())
    receipt = json.loads((root / "service-process.json").read_text())
    source = Path(receipt["source_root"])
    if not source.is_relative_to(root) or not (source.parent / "deployment.json").is_file():
        raise ValueError("Require the source overlay deployment receipt")
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != receipt["config_sha256"]:
        raise ValueError("Service config changed after deployment")
    proc = Path("/proc") / str(receipt["pid"])
    argv = proc.joinpath("cmdline").read_bytes().decode().split("\0")
    if "resources_servers.spreadsheet.service.app" not in argv or str(config_path) not in argv:
        raise ValueError("Service PID identity changed")
    process_env = dict(x.split("=", 1) for x in proc.joinpath("environ").read_bytes().decode().split("\0") if "=" in x)
    if process_env.get("PYTHONPATH") != str(source):
        raise ValueError("Running process uses another source overlay")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{config['port']}/health", timeout=5) as response:
        health = json.load(response)
    if health.get("max_pending") != 4000:
        raise ValueError("Running service does not advertise queue 4000")
    qa = source.parent / "qa"
    qa.mkdir(mode=0o700, exist_ok=False)
    env = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1",
               SPREADSHEET_TEST_REAL_LO="1", SPREADSHEET_TEST_ROOTFS=str(config["python_rootfs"]))
    env.pop("PYTHONHOME", None)
    command = [argv[0], "-B", "-m", "pytest", "-c", "/dev/null", "-p", "no:cacheprovider",
               str(patches / "test_reward_readers.py"), str(patches / "test_integration.py"),
               "--basetemp", str(qa / "pytest"), "-q"]
    with (qa / "linux-tests.log").open("xb") as log:
        completed = subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
    print((qa / "linux-tests.log").read_text(), flush=True)
    completed.check_returncode()
    manifest = Path(config["manifest"])
    command = [argv[0], "-B", str(patches / "check_live_service.py"),
               "--service-config", str(config_path),
               "--dataset", str(manifest.parent.parent / "nemo_gym/train.jsonl"),
               "--manifest", str(manifest), "--data-root", str(config["data_root"]),
               "--output-dir", str(qa / "live-api"), "--expected-hostname", args.expected_hostname]
    subprocess.run(command, cwd=source, env=env, check=True, timeout=900)
    result = {"event": "SERVICE_UPDATE_VERIFIED", "pid": receipt["pid"], "health": health,
              "source_root": str(source), "linux_log": str(qa / "linux-tests.log"),
              "api_report": str(qa / "live-api/report.json"), "model_rollouts_tested": False}
    (qa / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
