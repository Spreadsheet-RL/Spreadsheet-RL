from __future__ import annotations

import asyncio
import csv
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path


class WorkerProtocolError(RuntimeError):
    pass


class WorkerDiedError(RuntimeError):
    pass


def _format_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _get_windows_excel_recycle_private_mb() -> int:
    value = os.environ.get("REWARD_API_WINDOWS_EXCEL_RECYCLE_PRIVATE_MB")
    if value is None or not value.strip():
        return 4096
    try:
        n = int(value)
        return max(0, n)
    except ValueError:
        return 4096


def _get_windows_excel_recycle_jobs() -> int:
    value = os.environ.get("REWARD_API_WINDOWS_EXCEL_RECYCLE_JOBS")
    if value is None or not value.strip():
        return 0
    try:
        n = int(value)
        return max(0, n)
    except ValueError:
        return 0


def _process_private_bytes(pid: int) -> int | None:
    if os.name != "nt":
        return None
    if pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        get_process_memory_info = psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        get_process_memory_info.restype = wintypes.BOOL

        handle = None
        for access in (0x1000, 0x0400):
            handle = open_process(access, False, int(pid))
            if handle:
                break
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            ok = bool(
                get_process_memory_info(
                    handle,
                    ctypes.byref(counters),
                    wintypes.DWORD(ctypes.sizeof(counters)),
                )
            )
            if not ok:
                return None
            return int(counters.PrivateUsage)
        finally:
            close_handle(handle)
    except Exception:
        return None


def _creationflags_no_window() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def _windows_powershell_creation_time(pid: int) -> int | None:
    if os.name != "nt":
        return None
    if pid <= 0:
        return None
    try:
        kwargs = {}
        flags = _creationflags_no_window()
        if flags:
            kwargs["creationflags"] = flags
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Process -Id {int(pid)} -ErrorAction Stop).StartTime.ToFileTimeUtc()",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        text = (completed.stdout or "").strip()
        if not text:
            return None
        return int(text)
    except Exception:
        return None


def _process_creation_time(pid: int) -> int | None:
    if os.name != "nt":
        return None
    if pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE

        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        get_process_times.restype = wintypes.BOOL

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = None
        for access in (0x1000, 0x0400):
            handle = open_process(access, False, int(pid))
            if handle:
                break
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = bool(
                get_process_times(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
            )
            if not ok:
                return None
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            close_handle(handle)
    except Exception:
        return None


def _tasklist_image_name(pid: int) -> str | None:
    if os.name != "nt":
        return None
    if pid <= 0:
        return None
    try:
        kwargs = {}
        flags = _creationflags_no_window()
        if flags:
            kwargs["creationflags"] = flags
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        text = (completed.stdout or "").strip()
        if not text:
            return None
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        line = lines[0]
        if line.upper().startswith("INFO:"):
            return None
        row = next(csv.reader([line]))
        if not row:
            return None
        return row[0]
    except Exception:
        return None


def _taskkill_pid(pid: int, expected_creation_time: int | None = None) -> None:
    if os.name != "nt":
        return
    if pid <= 0:
        return
    if expected_creation_time is None:
        return
    current = _process_creation_time(pid)
    if current is None:
        current = _windows_powershell_creation_time(pid)
    if current is None or current != expected_creation_time:
        return
    image = _tasklist_image_name(pid)
    if (image or "").strip().upper() != "EXCEL.EXE":
        return
    try:
        kwargs = {}
        flags = _creationflags_no_window()
        if flags:
            kwargs["creationflags"] = flags
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            **kwargs,
        )
        for _ in range(20):
            if _tasklist_image_name(pid) is None:
                break
            time.sleep(0.25)
    except Exception:
        return


def _taskkill_tree(pid: int) -> None:
    if os.name != "nt":
        return
    if pid <= 0:
        return
    try:
        kwargs = {}
        flags = _creationflags_no_window()
        if flags:
            kwargs["creationflags"] = flags
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            **kwargs,
        )
    except Exception:
        return


@dataclass(frozen=True)
class WorkerResult:
    ok: bool
    reward: float
    msg: str


class ExcelWorkerProcess:
    def __init__(
        self,
        *,
        platform: str,
        loop: asyncio.AbstractEventLoop,
        startup_timeout_s: float = 30.0,
    ) -> None:
        self._platform = platform
        self._loop = loop
        self._startup_timeout_s = float(startup_timeout_s)
        self._proc: subprocess.Popen[str] | None = None
        self._rx: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._lock = asyncio.Lock()

        self.worker_pid: int | None = None
        self.excel_pid: int | None = None
        self.excel_creation_time: int | None = None
        self.jobs_run = 0

    @property
    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    async def start(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "async_reward_api.excel_worker_server",
            "--platform",
            self._platform,
        ]
        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=os.environ.copy(),
        )
        flags = _creationflags_no_window()
        if flags:
            kwargs["creationflags"] = flags

        self._proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603,S607 - controlled local command
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        ready = await asyncio.wait_for(self._rx.get(), timeout=self._startup_timeout_s)
        if ready.get("type") != "ready":
            raise WorkerProtocolError(f"Expected ready message, got: {ready!r}")

        worker_pid = ready.get("worker_pid")
        excel_pid = ready.get("excel_pid")
        self.worker_pid = int(worker_pid) if isinstance(worker_pid, int) else None
        self.excel_pid = int(excel_pid) if isinstance(excel_pid, int) else None
        if self.excel_pid is None:
            raise WorkerProtocolError(f"worker did not report excel_pid: {ready!r}")
        self.excel_creation_time = await asyncio.to_thread(_process_creation_time, self.excel_pid)
        if self.excel_creation_time is None:
            self.excel_creation_time = await asyncio.to_thread(_windows_powershell_creation_time, self.excel_pid)

    async def shutdown(self, *, force: bool = False) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            if force and self.excel_pid is not None:
                await asyncio.to_thread(_taskkill_pid, self.excel_pid, self.excel_creation_time)
            return

        if force and proc.poll() is None:
            await asyncio.to_thread(_taskkill_tree, int(proc.pid))

        if not force and proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
                    proc.stdin.flush()
                await asyncio.to_thread(proc.wait, timeout=10)
                return
            except Exception:
                force = True

        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            await asyncio.to_thread(proc.wait, timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.to_thread(proc.wait, timeout=5)
            except Exception:
                pass

        if force and self.excel_pid is not None:
            await asyncio.to_thread(_taskkill_pid, self.excel_pid, self.excel_creation_time)

    async def run_job(
        self,
        *,
        job_id: str,
        gt_file: Path,
        proc_file: Path,
        answer_position: str,
        timeout_s: float,
    ) -> WorkerResult:
        async with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise WorkerDiedError("worker process is not running")
            if proc.stdin is None:
                raise WorkerProtocolError("worker stdin is not available")

            req = {
                "type": "job",
                "job_id": job_id,
                "gt_file": str(gt_file),
                "proc_file": str(proc_file),
                "answer_position": answer_position,
            }
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            proc.stdin.flush()

            deadline = self._loop.time() + float(timeout_s)
            while True:
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()

                msg = await asyncio.wait_for(self._rx.get(), timeout=remaining)
                msg_type = msg.get("type")
                if msg_type == "exit":
                    returncode = msg.get("returncode")
                    stderr_tail = "\n".join(self._stderr_tail)
                    raise WorkerDiedError(
                        f"worker exited (returncode={returncode}); stderr_tail:\n{stderr_tail}"
                    )
                if msg_type != "result":
                    continue

                if str(msg.get("job_id") or "") != job_id:
                    continue

                ok = bool(msg.get("ok"))
                msg_text = str(msg.get("msg") or "")
                reward = 0.0
                if ok:
                    try:
                        reward = float(msg.get("reward") or 0.0)
                    except (TypeError, ValueError) as exc:
                        raise WorkerProtocolError("invalid worker reward") from exc
                self.jobs_run += 1
                return WorkerResult(ok=ok, reward=reward, msg=msg_text)

    async def run_recalc(
        self,
        *,
        job_id: str,
        proc_file: Path,
        timeout_s: float,
    ) -> WorkerResult:
        async with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise WorkerDiedError("worker process is not running")
            if proc.stdin is None:
                raise WorkerProtocolError("worker stdin is not available")

            req = {
                "type": "recalc",
                "job_id": job_id,
                "proc_file": str(proc_file),
            }
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            proc.stdin.flush()

            deadline = self._loop.time() + float(timeout_s)
            while True:
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()

                msg = await asyncio.wait_for(self._rx.get(), timeout=remaining)
                msg_type = msg.get("type")
                if msg_type == "exit":
                    returncode = msg.get("returncode")
                    stderr_tail = "\n".join(self._stderr_tail)
                    raise WorkerDiedError(
                        f"worker exited (returncode={returncode}); stderr_tail:\n{stderr_tail}"
                    )
                if msg_type != "result":
                    continue

                if str(msg.get("job_id") or "") != job_id:
                    continue

                ok = bool(msg.get("ok"))
                msg_text = str(msg.get("msg") or "")
                self.jobs_run += 1
                return WorkerResult(ok=ok, reward=0.0, msg=msg_text)

    def _stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                self._loop.call_soon_threadsafe(self._rx.put_nowait, msg)
        finally:
            self._loop.call_soon_threadsafe(
                self._rx.put_nowait,
                {"type": "exit", "returncode": proc.poll()},
            )

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.rstrip()
            if line:
                self._stderr_tail.append(line)


class ExcelWorkerPool:
    def __init__(
        self,
        *,
        size: int,
        platform: str,
        startup_timeout_s: float = 30.0,
    ) -> None:
        if size < 1:
            raise ValueError("size must be >= 1")
        self._size = int(size)
        self._platform = platform
        self._startup_timeout_s = float(startup_timeout_s)
        self._available: asyncio.Queue[ExcelWorkerProcess] = asyncio.Queue()
        self._workers: list[ExcelWorkerProcess] = []
        self._closing = False
        self._restart_tasks: dict[int, asyncio.Task[None]] = {}
        self._recycle_tasks: dict[int, asyncio.Task[None]] = {}
        self._recycle_private_mb = _get_windows_excel_recycle_private_mb()
        self._recycle_jobs = _get_windows_excel_recycle_jobs()

    @property
    def size(self) -> int:
        return self._size

    def status(self) -> dict[str, object]:
        alive_instances = sum(1 for worker in self._workers if worker.is_running)
        return {
            "enabled": not self._closing,
            "mode": "persistent",
            "configured_instances": self._size,
            "slots": len(self._workers),
            "active_instances": alive_instances,
            "alive_instances": alive_instances,
            "available_instances": self._available.qsize(),
            "restart_pending": sum(1 for task in self._restart_tasks.values() if not task.done()),
            "recycle_pending": sum(1 for task in self._recycle_tasks.values() if not task.done()),
        }

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        started: list[ExcelWorkerProcess] = []
        try:
            for _ in range(self._size):
                worker = ExcelWorkerProcess(
                    platform=self._platform,
                    loop=loop,
                    startup_timeout_s=self._startup_timeout_s,
                )
                try:
                    await worker.start()
                except Exception:
                    try:
                        await worker.shutdown(force=True)
                    except Exception:
                        pass
                    raise
                self._workers.append(worker)
                started.append(worker)
                await self._available.put(worker)
        except Exception:
            for worker in started:
                try:
                    await worker.shutdown(force=True)
                except Exception:
                    continue
            self._workers.clear()
            while True:
                try:
                    self._available.get_nowait()
                except asyncio.QueueEmpty:
                    break
            raise

    async def shutdown(self, *, force: bool = False) -> None:
        self._closing = True
        if self._recycle_tasks:
            await asyncio.gather(*self._recycle_tasks.values(), return_exceptions=True)
            self._recycle_tasks.clear()
        for task in list(self._restart_tasks.values()):
            task.cancel()
        if self._restart_tasks:
            await asyncio.gather(*self._restart_tasks.values(), return_exceptions=True)
            self._restart_tasks.clear()
        for worker in list(self._workers):
            try:
                await worker.shutdown(force=force)
            except Exception:
                continue

    def _schedule_restart(self, idx: int, *, expected: ExcelWorkerProcess) -> None:
        if self._closing:
            return
        existing = self._restart_tasks.get(idx)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._restart_slot(idx, expected=expected))
        self._restart_tasks[idx] = task

    def _schedule_recycle(self, idx: int, *, expected: ExcelWorkerProcess, reason: str) -> None:
        if self._closing:
            return
        existing = self._recycle_tasks.get(idx)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._recycle_slot(idx, expected=expected, reason=reason))
        self._recycle_tasks[idx] = task

    async def _restart_slot(self, idx: int, *, expected: ExcelWorkerProcess) -> None:
        delay_s = 0.5
        try:
            while not self._closing:
                replacement = ExcelWorkerProcess(
                    platform=self._platform,
                    loop=asyncio.get_running_loop(),
                    startup_timeout_s=self._startup_timeout_s,
                )
                try:
                    await replacement.start()
                except asyncio.CancelledError:
                    await replacement.shutdown(force=True)
                    raise
                except Exception as exc:  # noqa: BLE001
                    try:
                        await replacement.shutdown(force=True)
                    except Exception:
                        pass
                    print(
                        f"[_restart_slot] worker start failed, retrying: {_format_exception(exc)}",
                        file=sys.stderr,
                        flush=True,
                    )
                    await asyncio.sleep(delay_s)
                    delay_s = min(delay_s * 2.0, 10.0)
                    continue

                if self._closing:
                    await replacement.shutdown(force=True)
                    return

                if idx >= len(self._workers) or self._workers[idx] is not expected:
                    await replacement.shutdown(force=True)
                    return

                self._workers[idx] = replacement
                await self._available.put(replacement)
                return
        finally:
            self._restart_tasks.pop(idx, None)

    async def _recycle_slot(self, idx: int, *, expected: ExcelWorkerProcess, reason: str) -> None:
        try:
            if self._closing:
                return
            if idx >= len(self._workers) or self._workers[idx] is not expected:
                return
            print(f"[excel_pool] recycling worker: {reason}", file=sys.stderr, flush=True)
            try:
                await expected.shutdown(force=True)
            except Exception:
                pass
            if self._closing:
                return
            if idx >= len(self._workers) or self._workers[idx] is not expected:
                return
            self._schedule_restart(idx, expected=expected)
        finally:
            self._recycle_tasks.pop(idx, None)

    async def acquire(self, *, timeout_s: float | None = None) -> ExcelWorkerProcess:
        if timeout_s is None:
            return await self._available.get()
        return await asyncio.wait_for(self._available.get(), timeout=float(timeout_s))

    async def release(self, worker: ExcelWorkerProcess) -> None:
        if self._closing:
            return
        await self._available.put(worker)

    async def run_job(
        self,
        *,
        job_id: str,
        gt_file: Path,
        proc_file: Path,
        answer_position: str,
        timeout_s: float,
    ) -> tuple[float, str]:
        worker = await self.acquire(timeout_s=timeout_s)
        return await self.run_job_with_worker(
            worker,
            job_id=job_id,
            gt_file=gt_file,
            proc_file=proc_file,
            answer_position=answer_position,
            timeout_s=timeout_s,
        )

    async def run_job_with_worker(
        self,
        worker: ExcelWorkerProcess,
        *,
        job_id: str,
        gt_file: Path,
        proc_file: Path,
        answer_position: str,
        timeout_s: float,
    ) -> tuple[float, str]:
        return_worker: ExcelWorkerProcess | None = worker

        async def _replace_failed_worker(err_msg: str, *, failed: ExcelWorkerProcess) -> str:
            nonlocal worker, return_worker
            try:
                idx = self._workers.index(failed)
            except ValueError:
                idx = None

            return_worker = None
            try:
                await failed.shutdown(force=True)
            except Exception:
                pass

            if self._closing:
                return err_msg

            if idx is None:
                return err_msg

            replacement = ExcelWorkerProcess(
                platform=self._platform,
                loop=asyncio.get_running_loop(),
                startup_timeout_s=self._startup_timeout_s,
            )
            try:
                await replacement.start()
            except asyncio.CancelledError:
                await replacement.shutdown(force=True)
                raise
            except Exception as exc:  # noqa: BLE001
                try:
                    await replacement.shutdown(force=True)
                except Exception:
                    pass
                print(
                    f"[excel_pool] worker restart failed after {err_msg}: {_format_exception(exc)}",
                    file=sys.stderr,
                    flush=True,
                )
                self._schedule_restart(idx, expected=failed)
                return f"{err_msg}; restart failed"

            if self._closing:
                await replacement.shutdown(force=True)
                return err_msg

            if idx >= len(self._workers) or self._workers[idx] is not failed:
                await replacement.shutdown(force=True)
                return err_msg

            self._workers[idx] = replacement
            worker = replacement
            return_worker = replacement
            return err_msg

        try:
            try:
                result = await worker.run_job(
                    job_id=job_id,
                    gt_file=gt_file,
                    proc_file=proc_file,
                    answer_position=answer_position,
                    timeout_s=timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                err_msg = await _replace_failed_worker(
                    f"timeout after {timeout_s:.0f}s",
                    failed=worker,
                )
                raise RuntimeError(err_msg)
            except Exception as exc:  # noqa: BLE001
                print(f"[excel_pool] worker error: {_format_exception(exc)}", file=sys.stderr, flush=True)
                err_msg = await _replace_failed_worker(
                    "worker error",
                    failed=worker,
                )
                raise RuntimeError(err_msg) from exc

            if not result.ok:
                if result.msg:
                    print(
                        f"[excel_pool] worker reported failure: {result.msg}",
                        file=sys.stderr,
                        flush=True,
                    )
                err_msg = await _replace_failed_worker(
                    "worker reported failure",
                    failed=worker,
                )
                raise RuntimeError(err_msg)

            recycle_reason = None
            if self._recycle_jobs > 0 and worker.jobs_run >= self._recycle_jobs:
                recycle_reason = f"jobs_run={worker.jobs_run} >= {self._recycle_jobs}"
            if (
                recycle_reason is None
                and self._recycle_private_mb > 0
                and worker.excel_pid is not None
                and os.name == "nt"
            ):
                private_bytes = await asyncio.to_thread(_process_private_bytes, int(worker.excel_pid))
                if private_bytes is not None:
                    private_mb = private_bytes / 1024 / 1024
                    if private_mb >= float(self._recycle_private_mb):
                        recycle_reason = f"private_mb={private_mb:.0f} >= {self._recycle_private_mb}"
            if recycle_reason is not None:
                try:
                    idx = self._workers.index(worker)
                except ValueError:
                    idx = None
                if not self._closing and idx is not None:
                    return_worker = None
                    self._schedule_recycle(idx, expected=worker, reason=recycle_reason)
            return float(result.reward), result.msg
        finally:
            if not self._closing and return_worker is not None:
                await self._available.put(return_worker)

    async def recalc_file(
        self,
        *,
        proc_file: Path,
        timeout_s: float,
    ) -> tuple[bool, str]:
        worker = await self.acquire(timeout_s=timeout_s)
        job_id = uuid.uuid4().hex
        return await self.recalc_file_with_worker(
            worker,
            job_id=job_id,
            proc_file=proc_file,
            timeout_s=timeout_s,
        )

    async def recalc_file_with_worker(
        self,
        worker: ExcelWorkerProcess,
        *,
        job_id: str,
        proc_file: Path,
        timeout_s: float,
    ) -> tuple[bool, str]:
        return_worker: ExcelWorkerProcess | None = worker

        async def _replace_failed_worker(err_msg: str, *, failed: ExcelWorkerProcess) -> tuple[bool, str]:
            nonlocal worker, return_worker
            try:
                idx = self._workers.index(failed)
            except ValueError:
                idx = None

            return_worker = None
            try:
                await failed.shutdown(force=True)
            except Exception:
                pass

            if self._closing:
                return False, err_msg

            if idx is None:
                return False, err_msg

            replacement = ExcelWorkerProcess(
                platform=self._platform,
                loop=asyncio.get_running_loop(),
                startup_timeout_s=self._startup_timeout_s,
            )
            try:
                await replacement.start()
            except asyncio.CancelledError:
                await replacement.shutdown(force=True)
                raise
            except Exception as exc:  # noqa: BLE001
                try:
                    await replacement.shutdown(force=True)
                except Exception:
                    pass
                print(
                    f"[excel_pool] recalc worker restart failed after {err_msg}: {_format_exception(exc)}",
                    file=sys.stderr,
                    flush=True,
                )
                self._schedule_restart(idx, expected=failed)
                return False, f"{err_msg}; restart failed"

            if self._closing:
                await replacement.shutdown(force=True)
                return False, err_msg

            if idx >= len(self._workers) or self._workers[idx] is not failed:
                await replacement.shutdown(force=True)
                return False, err_msg

            self._workers[idx] = replacement
            worker = replacement
            return_worker = replacement
            return False, err_msg

        try:
            result = await worker.run_recalc(
                job_id=job_id,
                proc_file=proc_file,
                timeout_s=timeout_s,
            )
            if not result.ok:
                if result.msg:
                    print(
                        f"[excel_pool] recalc worker reported failure: {result.msg}",
                        file=sys.stderr,
                        flush=True,
                    )
                return await _replace_failed_worker(
                    "worker reported failure",
                    failed=worker,
                )
            recycle_reason = None
            if self._recycle_jobs > 0 and worker.jobs_run >= self._recycle_jobs:
                recycle_reason = f"jobs_run={worker.jobs_run} >= {self._recycle_jobs}"
            if (
                recycle_reason is None
                and self._recycle_private_mb > 0
                and worker.excel_pid is not None
                and os.name == "nt"
            ):
                private_bytes = await asyncio.to_thread(_process_private_bytes, int(worker.excel_pid))
                if private_bytes is not None:
                    private_mb = private_bytes / 1024 / 1024
                    if private_mb >= float(self._recycle_private_mb):
                        recycle_reason = f"private_mb={private_mb:.0f} >= {self._recycle_private_mb}"
            if recycle_reason is not None:
                try:
                    idx = self._workers.index(worker)
                except ValueError:
                    idx = None
                if not self._closing and idx is not None:
                    return_worker = None
                    self._schedule_recycle(idx, expected=worker, reason=recycle_reason)
            return True, result.msg
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return await _replace_failed_worker(
                f"timeout after {timeout_s:.0f}s",
                failed=worker,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[excel_pool] recalc worker error: {_format_exception(exc)}", file=sys.stderr, flush=True)
            return await _replace_failed_worker(
                "worker error",
                failed=worker,
            )
        finally:
            if not self._closing and return_worker is not None:
                await self._available.put(return_worker)
