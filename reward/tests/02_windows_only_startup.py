from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from _tempdir import temporary_directory


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_non_windows_rejected(override_value: str | None) -> None:
    env = os.environ.copy()
    env.pop("REWARD_API_PLATFORM", None)
    if override_value is None:
        env.pop("REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS", None)
    else:
        env["REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"] = override_value

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "async_reward_api.cli",
            "--platform",
            "windows",
            "--host",
            "127.0.0.1",
            "--port",
            str(_pick_free_port()),
            "--workers",
            "1",
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0:
        raise AssertionError("non-Windows host unexpectedly started the API")
    if "Windows-only" not in output:
        raise AssertionError(f"expected Windows-only error, got: {output!r}")


def main() -> int:
    if sys.platform.startswith(("win", "cygwin", "msys")):
        port = _pick_free_port()
        base_url = f"http://127.0.0.1:{port}"
        with temporary_directory(prefix="async_reward_api_startup_") as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env.pop("REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS", None)
            env["REWARD_API_OUTPUT_ROOT"] = str(tmp_path / "output_root")
            env["REWARD_API_DB_PATH"] = str(tmp_path / "jobs.sqlite3")
            env["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
            env["REWARD_API_INSTANCE_PER_WORKER"] = "0"
            proc = subprocess.Popen(  # noqa: S603,S607 - controlled local command
                [
                    sys.executable,
                    "-m",
                    "async_reward_api.cli",
                    "--platform",
                    "windows",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--workers",
                    "1",
                    "--log-level",
                    "warning",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.time() + 20
                while time.time() < deadline:
                    if proc.poll() is not None:
                        stdout, stderr = proc.communicate(timeout=2)
                        raise AssertionError(
                            "Windows API exited before /health responded\n"
                            f"stdout:\n{stdout}\n"
                            f"stderr:\n{stderr}"
                        )
                    try:
                        with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                            status = int(resp.status)
                            payload = json.loads(resp.read().decode("utf-8"))
                    except (OSError, ValueError, urllib.error.URLError):
                        time.sleep(0.2)
                        continue
                    if status == 200 and payload.get("status") == "ok":
                        print("OK: Windows host starts the API")
                        return 0
                    raise AssertionError(f"unexpected health payload: {payload}")
                raise AssertionError("Windows API did not become healthy")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
    _assert_non_windows_rejected(None)
    _assert_non_windows_rejected("FALSE")
    _assert_non_windows_rejected("No")

    print("OK: non-Windows startup is rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
