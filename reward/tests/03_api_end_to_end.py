from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openpyxl

from async_reward_api.platform import Platform
from async_reward_api.windows_process import (
    _kill_subprocess_tree,
    _list_excel_pids,
    _process_creation_time_with_fallback,
    _process_matches_creation_time,
)
from _tempdir import temporary_directory


def _write_workbook(path: Path, *, value: object) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = value
    wb.save(path)
    wb.close()


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _graceful_windows_shutdown(proc: subprocess.Popen[str]) -> bool:
    if os.name != "nt" or proc.poll() is not None:
        return proc.poll() == 0

    signal_helper = r"""
import ctypes
import sys
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.FreeConsole.argtypes = []
kernel32.FreeConsole.restype = wintypes.BOOL
kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.AttachConsole.restype = wintypes.BOOL
handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL

kernel32.FreeConsole()
if not kernel32.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
ignore_ctrl_event = handler_type(lambda _event: True)
if not kernel32.SetConsoleCtrlHandler(ignore_ctrl_event, True):
    raise ctypes.WinError(ctypes.get_last_error())
if not kernel32.GenerateConsoleCtrlEvent(1, 0):
    raise ctypes.WinError(ctypes.get_last_error())
time.sleep(1.0)
kernel32.FreeConsole()
"""
    try:
        completed = subprocess.run(  # noqa: S603 - controlled helper and PID
            [sys.executable, "-c", signal_helper, str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _excel_process_identities() -> dict[int, int]:
    pids = _list_excel_pids(Platform.WINDOWS)
    if pids is None:
        raise RuntimeError("Excel PID enumeration is unavailable")

    identities: dict[int, int] = {}
    for pid in sorted(pids):
        creation_time = _process_creation_time_with_fallback(pid)
        if creation_time is None:
            refreshed_pids = _list_excel_pids(Platform.WINDOWS)
            if refreshed_pids is None:
                raise RuntimeError("Excel PID enumeration became unavailable")
            if pid not in refreshed_pids:
                continue
            raise RuntimeError(f"Excel creation-time lookup is unavailable for PID {pid}")
        identities[pid] = creation_time
    return identities


def _wait_for_new_excel_processes(
    *,
    baseline: dict[int, int],
    expected_count: int,
    timeout_s: float,
) -> dict[int, int]:
    deadline = time.monotonic() + timeout_s
    observed: dict[int, int] = {}
    while time.monotonic() < deadline:
        current = _excel_process_identities()
        observed = {
            pid: creation_time
            for pid, creation_time in current.items()
            if baseline.get(pid) != creation_time
        }
        if len(observed) >= expected_count:
            return observed
        time.sleep(0.2)
    raise RuntimeError(
        f"expected at least {expected_count} new Excel processes, observed {len(observed)}: {observed}"
    )


def _wait_for_excel_processes_to_exit(
    identities: dict[int, int],
    *,
    timeout_s: float,
) -> dict[int, int]:
    deadline = time.monotonic() + timeout_s
    remaining = dict(identities)
    while remaining and time.monotonic() < deadline:
        current_pids = _list_excel_pids(Platform.WINDOWS)
        if current_pids is None:
            raise RuntimeError("Excel PID enumeration is unavailable during shutdown verification")

        next_remaining: dict[int, int] = {}
        for pid, expected_creation_time in remaining.items():
            if pid not in current_pids:
                continue
            if _process_matches_creation_time(pid, expected_creation_time):
                next_remaining[pid] = expected_creation_time
                continue

            current_creation_time = _process_creation_time_with_fallback(pid)
            if current_creation_time is None:
                refreshed_pids = _list_excel_pids(Platform.WINDOWS)
                if refreshed_pids is None:
                    raise RuntimeError("Excel PID enumeration became unavailable")
                if pid in refreshed_pids:
                    raise RuntimeError(f"Excel creation-time lookup is unavailable for PID {pid}")
        remaining = next_remaining
        if remaining:
            time.sleep(0.2)
    return remaining


def _http_json(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def _build_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----async_reward_api_{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode())
        chunks.append(b"\r\n")

    for name, (filename, content, content_type) in files.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    return body, boundary


def _submit_reward(base_url: str, *, thread_dir: str, upload_bytes: bytes) -> dict:
    body, boundary = _build_multipart(
        {"thread_dir": thread_dir},
        {
            "file": (
                "output.xlsx",
                upload_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    return _http_json(
        "POST",
        f"{base_url}/reward/submit",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end API smoke test (submit + poll).")
    parser.add_argument("--platform", choices=["windows"], default="windows")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--instance-per-worker", type=int, default=2)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--wait-s", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.instance_per_worker < 2:
        parser.error("--instance-per-worker must be at least 2 for the multi-slot pool test")
    if args.jobs < 2:
        parser.error("--jobs must be at least 2 for the concurrent job test")

    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}"

    with temporary_directory(prefix="async_reward_api_e2e_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        db_path = tmp_path / "jobs.sqlite3"
        thread_dirs = [f"thread_{idx}" for idx in range(1, args.jobs + 1)]
        for thread_dir in thread_dirs:
            sample_dir = output_root / thread_dir
            sample_dir.mkdir(parents=True, exist_ok=True)
            (sample_dir / "instruction.json").write_text(
                json.dumps({"answer_position": "Sheet1!A1"}, ensure_ascii=False),
                encoding="utf-8",
            )
            _write_workbook(sample_dir / "target.xlsx", value=123)

        env = os.environ.copy()
        env["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        env["REWARD_API_DB_PATH"] = str(db_path)
        env["REWARD_API_KEEP_FILES"] = "1"

        baseline_excel_identities = _excel_process_identities()
        server_excel_identities: dict[int, int] = {}

        cmd = [
            sys.executable,
            "-m",
            "async_reward_api.cli",
            "--platform",
            args.platform,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(max(1, args.workers)),
            "--instance-per-worker",
            str(args.instance_per_worker),
            "--log-level",
            "warning",
        ]
        print("starting server:", " ".join(cmd))
        proc = subprocess.Popen(  # noqa: S603,S607 - controlled local command
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 90.0
            last_err: str | None = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=2)
                    raise RuntimeError(
                        "server exited before becoming healthy\n"
                        f"stdout:\n{stdout}\n"
                        f"stderr:\n{stderr}"
                    )
                try:
                    health = _http_json("GET", f"{base_url}/health")
                except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    last_err = str(exc)
                    time.sleep(0.2)
                    continue

                excel_pool = health.get("excel_pool")
                if not isinstance(excel_pool, dict):
                    last_err = f"health did not include Excel pool status: {health}"
                    time.sleep(0.2)
                    continue
                configured_instances = int(excel_pool.get("configured_instances") or 0)
                slots = int(excel_pool.get("slots") or 0)
                alive_instances = int(excel_pool.get("alive_instances") or 0)
                available_instances = int(excel_pool.get("available_instances") or 0)
                restart_pending = int(excel_pool.get("restart_pending") or 0)
                recycle_pending = int(excel_pool.get("recycle_pending") or 0)
                fully_ready = (
                    health.get("status") == "ok"
                    and health.get("ready") is True
                    and int(health.get("instance_per_worker") or 0) == args.instance_per_worker
                    and excel_pool.get("enabled") is True
                    and excel_pool.get("mode") == "persistent"
                    and configured_instances == args.instance_per_worker
                    and slots >= 2
                    and alive_instances == configured_instances
                    and available_instances == configured_instances
                    and restart_pending == 0
                    and recycle_pending == 0
                )
                if fully_ready:
                    print("health:", health)
                    break
                last_err = f"Excel pool was not fully ready: {health}"
                time.sleep(0.2)
            else:
                raise RuntimeError(f"server did not become healthy (last error: {last_err})")

            expected_excel_processes = max(1, args.workers) * args.instance_per_worker
            server_excel_identities = _wait_for_new_excel_processes(
                baseline=baseline_excel_identities,
                expected_count=expected_excel_processes,
                timeout_s=90.0,
            )
            print("server Excel processes:", server_excel_identities)

            upload_bytes = (output_root / thread_dirs[0] / "target.xlsx").read_bytes()
            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                submit_futures = [
                    executor.submit(
                        _submit_reward,
                        base_url,
                        thread_dir=thread_dir,
                        upload_bytes=upload_bytes,
                    )
                    for thread_dir in thread_dirs
                ]
                submits = [future.result() for future in submit_futures]

            job_ids: list[str] = []
            for idx, submit in enumerate(submits, start=1):
                print(f"submit[{idx}]:", submit)
                job_id = submit.get("job_id")
                if not job_id:
                    raise RuntimeError(f"submit did not return job_id: {submit}")
                job_ids.append(str(job_id))

            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                result_futures = [
                    executor.submit(
                        _http_json,
                        "GET",
                        f"{base_url}/reward/result/{job_id}?wait_s={args.wait_s}",
                        timeout_s=args.wait_s + 10.0,
                    )
                    for job_id in job_ids
                ]
                results = [future.result() for future in result_futures]

            for idx, result in enumerate(results, start=1):
                print(f"result[{idx}]:", result)
                if result.get("status") != "done":
                    raise RuntimeError(f"job did not finish successfully: {result}")
                if float(result.get("reward") or 0.0) != 1.0:
                    raise RuntimeError(f"expected reward=1.0, got: {result}")
                if str(result.get("msg") or ""):
                    raise RuntimeError(f"expected empty result msg, got: {result}")
            return 0
        finally:
            body_exception = sys.exc_info()[1]
            graceful = _graceful_windows_shutdown(proc)
            if not graceful:
                if proc.poll() is None:
                    _kill_subprocess_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)

            cleanup_error: BaseException | None = None
            if graceful and server_excel_identities:
                try:
                    remaining_excel = _wait_for_excel_processes_to_exit(
                        server_excel_identities,
                        timeout_s=30.0,
                    )
                    if remaining_excel:
                        cleanup_error = RuntimeError(
                            f"graceful shutdown left server Excel processes running: {remaining_excel}"
                        )
                except BaseException as exc:  # noqa: BLE001
                    cleanup_error = exc

            if body_exception is None:
                if not graceful:
                    raise RuntimeError(
                        f"server did not shut down gracefully with exit code 0 (exit code: {proc.returncode})"
                    )
                if cleanup_error is not None:
                    raise cleanup_error
            else:
                if not graceful:
                    print(
                        f"server graceful shutdown failed during test failure (exit code: {proc.returncode})",
                        file=sys.stderr,
                    )
                if cleanup_error is not None:
                    print(f"server Excel cleanup verification failed: {cleanup_error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
