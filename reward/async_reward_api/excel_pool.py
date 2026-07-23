from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .aio import _run_cancellation_safe
from .messages import format_exception as _format_exception
from .messages import public_worker_message as _public_worker_message
from .windows_process import (
    _creationflags_no_window,
    _process_creation_time_with_fallback,
    _process_exists,
    _process_matches_creation_time,
    _process_private_bytes,
    _taskkill_pid,
    _taskkill_process_tree_if_matches,
    _taskkill_tree,
)


logger = logging.getLogger(__name__)


class WorkerProtocolError(RuntimeError):
    pass


class WorkerDiedError(RuntimeError):
    pass


_HEALTH_PROBE_TIMEOUT_S = 5.5
_HEALTH_STATUS_TTL_S = 3.0


def _result_msg_is_fatal_worker_error(msg: object) -> bool:
    return isinstance(msg, str) and msg.lstrip().lower().startswith("fatal worker error")


def _consume_task_exception(task: asyncio.Task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _write_worker_stdin(stdin, text: str) -> None:
    stdin.write(text)
    stdin.flush()


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


def _coerce_ready_pid(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return int(value)


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
        self._stderr_tail_lock = threading.Lock()
        self._lock = asyncio.Lock()

        self.worker_pid: int | None = None
        self.worker_creation_time: int | None = None
        self.excel_pid: int | None = None
        self.excel_creation_time: int | None = None
        self.jobs_run = 0

    @property
    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    async def is_healthy(self) -> bool:
        if not self.is_running:
            return False
        if self.excel_pid is None or self.excel_creation_time is None:
            return False
        return await asyncio.to_thread(
            _process_matches_creation_time,
            self.excel_pid,
            self.excel_creation_time,
        )

    async def _shutdown_after_start_protocol_error(self) -> None:
        try:
            await self.shutdown(force=False)
        except Exception:
            try:
                await self.shutdown(force=True)
            except Exception:
                pass

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

        try:
            ready = await asyncio.wait_for(self._rx.get(), timeout=self._startup_timeout_s)
            if ready.get("type") != "ready":
                await self._shutdown_after_start_protocol_error()
                raise WorkerProtocolError(f"Expected ready message, got: {ready!r}")

            worker_pid = ready.get("worker_pid")
            excel_pid = ready.get("excel_pid")
            self.worker_pid = _coerce_ready_pid(worker_pid)
            self.excel_pid = _coerce_ready_pid(excel_pid)
            proc_pid = _coerce_ready_pid(getattr(self._proc, "pid", None))
            if self.worker_pid is None or proc_pid is None:
                await self._shutdown_after_start_protocol_error()
                raise WorkerProtocolError(f"worker reported unexpected worker_pid: {ready!r}")
            self.worker_creation_time = await asyncio.to_thread(
                _process_creation_time_with_fallback,
                self.worker_pid,
            )
            if self.worker_pid != proc_pid and self.worker_creation_time is None:
                await self._shutdown_after_start_protocol_error()
                raise WorkerProtocolError(f"worker reported unexpected worker_pid: {ready!r}")
            if self.excel_pid is None:
                await self._shutdown_after_start_protocol_error()
                raise WorkerProtocolError(f"worker did not report excel_pid: {ready!r}")
            self.excel_creation_time = await asyncio.to_thread(
                _process_creation_time_with_fallback,
                self.excel_pid,
            )
            if self.excel_creation_time is None:
                await self._shutdown_after_start_protocol_error()
                raise WorkerProtocolError(f"worker did not report verifiable excel_pid: {ready!r}")
        except asyncio.CancelledError:
            await self.shutdown(force=True)
            raise
        except Exception:
            await self.shutdown(force=True)
            raise

    async def shutdown(self, *, force: bool = False) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            if force and self.worker_pid is not None:
                await asyncio.to_thread(
                    _taskkill_process_tree_if_matches,
                    self.worker_pid,
                    self.worker_creation_time,
                )
            if force and self.excel_pid is not None:
                await asyncio.to_thread(_taskkill_pid, self.excel_pid, self.excel_creation_time)
            return

        async def _force_cleanup() -> None:
            if proc.poll() is None:
                try:
                    await asyncio.to_thread(_taskkill_tree, int(proc.pid))
                except Exception:
                    pass
            if self.worker_pid is not None and self.worker_pid != proc.pid:
                try:
                    await asyncio.to_thread(
                        _taskkill_process_tree_if_matches,
                        self.worker_pid,
                        self.worker_creation_time,
                    )
                except Exception:
                    pass
            if self.excel_pid is not None:
                try:
                    await asyncio.to_thread(_taskkill_pid, self.excel_pid, self.excel_creation_time)
                except Exception:
                    pass

        cancel_requested = False
        if force and proc.poll() is None:
            try:
                await asyncio.to_thread(_taskkill_tree, int(proc.pid))
            except asyncio.CancelledError:
                cancel_requested = True
                await _force_cleanup()
            except Exception:
                pass

        if not force and proc.poll() is None:
            try:
                if proc.stdin is not None:
                    await self._write_request(
                        proc,
                        {"type": "shutdown"},
                        deadline=self._loop.time() + 1.0,
                    )
                await asyncio.to_thread(proc.wait, timeout=10)
                if self.excel_pid is not None and await asyncio.to_thread(_process_exists, self.excel_pid):
                    await asyncio.to_thread(_taskkill_pid, self.excel_pid, self.excel_creation_time)
                if cancel_requested:
                    raise asyncio.CancelledError
                return
            except asyncio.CancelledError:
                cancel_requested = True
                await _force_cleanup()
            except Exception:
                force = True

        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            await asyncio.to_thread(proc.wait, timeout=5)
        except asyncio.CancelledError:
            cancel_requested = True
            await _force_cleanup()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.to_thread(proc.wait, timeout=5)
            except asyncio.CancelledError:
                cancel_requested = True
                await _force_cleanup()
            except Exception:
                pass

        if force and self.excel_pid is not None:
            try:
                await asyncio.to_thread(_taskkill_pid, self.excel_pid, self.excel_creation_time)
            except asyncio.CancelledError:
                cancel_requested = True
                await _force_cleanup()
        if force and self.worker_pid is not None and self.worker_pid != proc.pid:
            try:
                await asyncio.to_thread(
                    _taskkill_process_tree_if_matches,
                    self.worker_pid,
                    self.worker_creation_time,
                )
            except asyncio.CancelledError:
                cancel_requested = True
                await _force_cleanup()

        if cancel_requested:
            raise asyncio.CancelledError

    async def _write_request(self, proc: subprocess.Popen[str], request: dict[str, object], *, deadline: float) -> None:
        if proc.stdin is None:
            raise WorkerProtocolError("worker stdin is not available")
        remaining = deadline - self._loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        write_task = asyncio.create_task(
            asyncio.to_thread(_write_worker_stdin, proc.stdin, json.dumps(request) + "\n")
        )
        write_task.add_done_callback(_consume_task_exception)
        await asyncio.wait_for(asyncio.shield(write_task), timeout=remaining)

    async def run_job(
        self,
        *,
        job_id: str,
        gt_file: Path,
        proc_file: Path,
        answer_position: str,
        timeout_s: float,
    ) -> WorkerResult:
        return await self._run_request(
            {
                "type": "job",
                "job_id": job_id,
                "gt_file": str(gt_file),
                "proc_file": str(proc_file),
                "answer_position": answer_position,
            },
            job_id=job_id,
            timeout_s=timeout_s,
            expect_reward=True,
        )

    async def run_recalc(
        self,
        *,
        job_id: str,
        proc_file: Path,
        timeout_s: float,
    ) -> WorkerResult:
        return await self._run_request(
            {
                "type": "recalc",
                "job_id": job_id,
                "proc_file": str(proc_file),
            },
            job_id=job_id,
            timeout_s=timeout_s,
            expect_reward=False,
        )

    async def _run_request(
        self,
        request: dict[str, object],
        *,
        job_id: str,
        timeout_s: float,
        expect_reward: bool,
    ) -> WorkerResult:
        async with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise WorkerDiedError("worker process is not running")

            deadline = self._loop.time() + float(timeout_s)
            await self._write_request(proc, request, deadline=deadline)
            while True:
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()

                msg = await asyncio.wait_for(self._rx.get(), timeout=remaining)
                msg_type = msg.get("type")
                if msg_type == "exit":
                    returncode = msg.get("returncode")
                    with self._stderr_tail_lock:
                        stderr_tail = "\n".join(self._stderr_tail)
                    raise WorkerDiedError(
                        f"worker exited (returncode={returncode}); stderr_tail:\n{stderr_tail}"
                    )
                if msg_type == "protocol_error":
                    raise WorkerProtocolError(str(msg.get("msg") or "invalid worker stdout"))
                if msg_type != "result":
                    continue

                if str(msg.get("job_id") or "") != job_id:
                    raise WorkerProtocolError("unexpected worker job_id")

                ok_raw = msg.get("ok")
                if not isinstance(ok_raw, bool):
                    raise WorkerProtocolError("invalid worker ok")
                ok = ok_raw
                msg_text = str(msg.get("msg") or "")
                reward = 0.0
                if expect_reward and ok:
                    if "reward" not in msg:
                        raise WorkerProtocolError("missing worker reward")
                    if isinstance(msg["reward"], bool):
                        raise WorkerProtocolError("invalid worker reward")
                    try:
                        reward = float(msg["reward"])
                    except (TypeError, ValueError) as exc:
                        raise WorkerProtocolError("invalid worker reward") from exc
                    if not math.isfinite(reward):
                        raise WorkerProtocolError("invalid worker reward")
                self.jobs_run += 1
                return WorkerResult(ok=ok, reward=reward, msg=msg_text)

    def _stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        def _put_message(message: dict[str, object]) -> bool:
            try:
                self._loop.call_soon_threadsafe(self._rx.put_nowait, message)
                return True
            except RuntimeError:
                return False

        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    _put_message({"type": "protocol_error", "msg": "invalid worker stdout"})
                    return
                if not isinstance(msg, dict):
                    _put_message({"type": "protocol_error", "msg": "invalid worker stdout"})
                    return
                if not _put_message(msg):
                    return
        finally:
            _put_message({"type": "exit", "returncode": proc.poll()})

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.rstrip()
            if line:
                with self._stderr_tail_lock:
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
        self._status_health_cache: dict[ExcelWorkerProcess, tuple[float, bool]] = {}

    @property
    def size(self) -> int:
        return self._size

    async def status(self) -> dict[str, object]:
        health_results = await asyncio.gather(
            *(self._worker_health_for_status_cached(worker) for worker in self._workers),
            return_exceptions=True,
        )
        alive_instances = sum(1 for result in health_results if result is True)
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

    @staticmethod
    def _worker_is_running(worker: ExcelWorkerProcess) -> bool:
        return bool(worker.is_running)

    @staticmethod
    async def _worker_is_healthy(worker: ExcelWorkerProcess) -> bool:
        return bool(await worker.is_healthy())

    @staticmethod
    async def _worker_health_for_status(worker: ExcelWorkerProcess) -> bool:
        try:
            return await asyncio.wait_for(
                ExcelWorkerPool._worker_is_healthy(worker),
                timeout=_HEALTH_PROBE_TIMEOUT_S,
            )
        except Exception:
            return False

    def _prune_health_cache(self) -> None:
        for key in list(self._status_health_cache):
            if not any(key is live_worker for live_worker in self._workers):
                self._status_health_cache.pop(key, None)

    def _forget_worker_health(self, worker: ExcelWorkerProcess) -> None:
        self._status_health_cache.pop(worker, None)

    def _record_worker_healthy_now(self, worker: ExcelWorkerProcess) -> None:
        if worker in self._workers and self._worker_is_running(worker):
            self._status_health_cache[worker] = (time.monotonic(), True)

    def _worker_recently_verified_healthy(self, worker: ExcelWorkerProcess) -> bool:
        self._prune_health_cache()
        if not self._worker_is_running(worker):
            self._forget_worker_health(worker)
            return False
        now = time.monotonic()
        cached = self._status_health_cache.get(worker)
        if cached is None:
            return False
        checked_at, healthy = cached
        if now - checked_at <= _HEALTH_STATUS_TTL_S:
            return bool(healthy)
        self._forget_worker_health(worker)
        return False

    async def _worker_health_for_status_cached(self, worker: ExcelWorkerProcess) -> bool:
        self._prune_health_cache()
        if not self._worker_is_running(worker):
            self._forget_worker_health(worker)
            return False
        cached = self._status_health_cache.get(worker)
        if cached is not None:
            checked_at, healthy = cached
            if time.monotonic() - checked_at <= _HEALTH_STATUS_TTL_S:
                return bool(healthy)
            self._forget_worker_health(worker)
        healthy = await self._worker_health_for_status(worker)
        if not self._worker_is_running(worker):
            self._forget_worker_health(worker)
            return False
        self._status_health_cache[worker] = (time.monotonic(), bool(healthy))
        return healthy

    def _schedule_worker_recycle(self, worker: ExcelWorkerProcess, *, reason: str) -> bool:
        try:
            idx = self._workers.index(worker)
        except ValueError:
            return False
        self._forget_worker_health(worker)
        self._schedule_recycle(idx, expected=worker, reason=reason)
        return True

    async def _worker_is_healthy_bounded(
        self,
        worker: ExcelWorkerProcess,
        *,
        timeout_s: float,
    ) -> bool:
        if timeout_s <= 0:
            raise asyncio.TimeoutError()
        return await asyncio.wait_for(self._worker_is_healthy(worker), timeout=timeout_s)

    def _requeue_owned_worker(self, worker: ExcelWorkerProcess) -> None:
        if self._closing or worker not in self._workers:
            return
        self._available.put_nowait(worker)

    def _drain_available(self) -> None:
        while True:
            try:
                self._available.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _shutdown_workers_parallel(
        self,
        workers: list[ExcelWorkerProcess],
        *,
        force: bool,
    ) -> bool:
        if not workers:
            return False

        tasks = [asyncio.create_task(worker.shutdown(force=force)) for worker in workers]
        gather_task = asyncio.gather(*tasks, return_exceptions=True)
        cancel_requested = False
        shutdown_tasks_cancelled = False
        while True:
            try:
                await asyncio.shield(gather_task)
                break
            except asyncio.CancelledError:
                cancel_requested = True
                if not shutdown_tasks_cancelled:
                    shutdown_tasks_cancelled = True
                    for task in tasks:
                        task.cancel()
                if gather_task.done():
                    break
        results = gather_task.result()
        return cancel_requested or any(isinstance(result, asyncio.CancelledError) for result in results)

    async def _force_shutdown_workers(self, workers: list[ExcelWorkerProcess]) -> bool:
        return await self._shutdown_workers_parallel(workers, force=True)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        candidates = [
            ExcelWorkerProcess(
                platform=self._platform,
                loop=loop,
                startup_timeout_s=self._startup_timeout_s,
            )
            for _ in range(self._size)
        ]
        start_tasks = [asyncio.create_task(worker.start()) for worker in candidates]
        try:
            pending = set(start_tasks)
            results_by_task: dict[asyncio.Task[None], BaseException | None] = {}
            successful_count = 0
            failed_count = 0
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    if task.cancelled():
                        result: BaseException | None = asyncio.CancelledError()
                    else:
                        result = task.exception()
                    results_by_task[task] = result
                    if result is None:
                        successful_count += 1
                    else:
                        failed_count += 1

                if successful_count > 0 and failed_count > 0 and pending:
                    pending_tasks = list(pending)
                    for task in pending_tasks:
                        task.cancel()
                    pending_results = await asyncio.gather(*pending_tasks, return_exceptions=True)
                    for task, result in zip(pending_tasks, pending_results, strict=True):
                        task_result = result if isinstance(result, BaseException) else None
                        results_by_task[task] = task_result
                    pending.clear()

            results = [results_by_task[task] for task in start_tasks]
            successful: list[ExcelWorkerProcess] = []
            failed: list[tuple[int, ExcelWorkerProcess, BaseException]] = []
            for idx, (worker, result) in enumerate(zip(candidates, results, strict=True)):
                if isinstance(result, BaseException):
                    failed.append((idx, worker, result))
                else:
                    successful.append(worker)

            if not successful:
                first_error = failed[0][2]
                raise first_error

            if failed:
                cleanup_cancelled = await self._force_shutdown_workers(
                    [worker for _, worker, _ in failed]
                )
                if cleanup_cancelled:
                    raise asyncio.CancelledError

            self._workers.extend(candidates)
            for worker in successful:
                await self._available.put(worker)
            for idx, worker, exc in failed:
                logger.warning(
                    f"[excel_pool] worker slot {idx} failed during startup; "
                    f"retrying in background: {_format_exception(exc)}"
                )
                self._schedule_restart(idx, expected=worker)
        except asyncio.CancelledError:
            for task in start_tasks:
                task.cancel()
            await asyncio.gather(*start_tasks, return_exceptions=True)
            await self._force_shutdown_workers(candidates)
            self._workers.clear()
            self._status_health_cache.clear()
            self._drain_available()
            raise
        except Exception:
            for task in start_tasks:
                task.cancel()
            await asyncio.gather(*start_tasks, return_exceptions=True)
            cleanup_cancelled = await self._force_shutdown_workers(candidates)
            self._workers.clear()
            self._status_health_cache.clear()
            self._drain_available()
            if cleanup_cancelled:
                raise asyncio.CancelledError
            raise

    async def shutdown(self, *, force: bool = False) -> None:
        self._closing = True
        cancel_requested = False

        async def _await_shutdown_step(awaitable) -> None:
            nonlocal cancel_requested
            while True:
                try:
                    await asyncio.shield(awaitable)
                    return
                except asyncio.CancelledError:
                    cancel_requested = True
                    if awaitable.done():
                        try:
                            awaitable.result()
                        except (asyncio.CancelledError, Exception):
                            pass
                        return
                except Exception:
                    return

        if self._recycle_tasks:
            await _await_shutdown_step(
                asyncio.gather(*self._recycle_tasks.values(), return_exceptions=True)
            )
            self._recycle_tasks.clear()
        for task in list(self._restart_tasks.values()):
            task.cancel()
        if self._restart_tasks:
            await _await_shutdown_step(
                asyncio.gather(*self._restart_tasks.values(), return_exceptions=True)
            )
            self._restart_tasks.clear()
        workers = list(self._workers)
        worker_shutdown_cancelled = await self._shutdown_workers_parallel(
            workers,
            force=force or cancel_requested,
        )
        cancel_requested = cancel_requested or worker_shutdown_cancelled
        self._workers.clear()
        self._status_health_cache.clear()
        self._drain_available()
        if cancel_requested:
            raise asyncio.CancelledError

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
        self._forget_worker_health(expected)
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
                    logger.warning(f"[_restart_slot] worker start failed, retrying: {_format_exception(exc)}")
                    await asyncio.sleep(delay_s)
                    delay_s = min(delay_s * 2.0, 10.0)
                    continue

                if self._closing:
                    await replacement.shutdown(force=True)
                    return

                if idx >= len(self._workers) or self._workers[idx] is not expected:
                    await replacement.shutdown(force=True)
                    return

                self._forget_worker_health(expected)
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
            logger.warning(f"[excel_pool] recycling worker: {reason}")
            self._forget_worker_health(expected)
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
        deadline = None
        if timeout_s is not None:
            deadline = asyncio.get_running_loop().time() + float(timeout_s)

        while True:
            if deadline is None:
                worker = await self._available.get()
            else:
                remaining_s = deadline - asyncio.get_running_loop().time()
                if remaining_s <= 0:
                    raise asyncio.TimeoutError()
                worker = await asyncio.wait_for(self._available.get(), timeout=remaining_s)

            if worker not in self._workers:
                continue
            health_timeout_s = _HEALTH_PROBE_TIMEOUT_S
            if deadline is not None:
                health_timeout_s = min(
                    health_timeout_s,
                    deadline - asyncio.get_running_loop().time(),
                )
            try:
                worker_is_healthy = await self._worker_is_healthy_bounded(
                    worker,
                    timeout_s=health_timeout_s,
                )
            except asyncio.TimeoutError:
                self._forget_worker_health(worker)
                self._schedule_worker_recycle(worker, reason="worker health probe timed out when acquired")
                continue
            except asyncio.CancelledError:
                self._requeue_owned_worker(worker)
                raise
            if worker not in self._workers:
                continue
            if worker_is_healthy:
                self._record_worker_healthy_now(worker)
                return worker

            self._forget_worker_health(worker)
            reason = (
                "Excel process was not running when acquired"
                if self._worker_is_running(worker)
                else "worker was not running when acquired"
            )
            self._schedule_worker_recycle(worker, reason=reason)

    async def release(self, worker: ExcelWorkerProcess) -> None:
        if self._closing:
            return
        if worker not in self._workers:
            return
        if self._worker_recently_verified_healthy(worker):
            await self._available.put(worker)
            return
        try:
            worker_is_healthy = await self._worker_is_healthy_bounded(
                worker,
                timeout_s=_HEALTH_PROBE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self._forget_worker_health(worker)
            self._schedule_worker_recycle(worker, reason="worker health probe timed out when released")
            return
        except asyncio.CancelledError:
            self._requeue_owned_worker(worker)
            raise
        except Exception:
            self._forget_worker_health(worker)
            self._schedule_worker_recycle(worker, reason="worker release failed")
            raise
        if not worker_is_healthy:
            self._forget_worker_health(worker)
            reason = (
                "Excel process was not running when released"
                if self._worker_is_running(worker)
                else "worker was not running when released"
            )
            self._schedule_worker_recycle(worker, reason=reason)
            return
        if worker not in self._workers:
            return
        self._record_worker_healthy_now(worker)
        await self._available.put(worker)

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
        result = await self._execute_with_worker(
            worker,
            run=lambda current: current.run_job(
                job_id=job_id,
                gt_file=gt_file,
                proc_file=proc_file,
                answer_position=answer_position,
                timeout_s=timeout_s,
            ),
            is_reward=True,
            timeout_s=timeout_s,
        )
        return float(result.reward), result.msg

    async def recalc_file_with_worker(
        self,
        worker: ExcelWorkerProcess,
        *,
        job_id: str,
        proc_file: Path,
        timeout_s: float,
    ) -> tuple[bool, str]:
        result = await self._execute_with_worker(
            worker,
            run=lambda current: current.run_recalc(
                job_id=job_id,
                proc_file=proc_file,
                timeout_s=timeout_s,
            ),
            is_reward=False,
            timeout_s=timeout_s,
        )
        return bool(result.ok), result.msg

    async def _replace_failed_worker(
        self,
        failed: ExcelWorkerProcess,
    ) -> None:
        try:
            idx = self._workers.index(failed)
        except ValueError:
            idx = None

        self._forget_worker_health(failed)
        try:
            await failed.shutdown(force=True)
        except asyncio.CancelledError:
            if not self._closing and idx is not None:
                self._schedule_restart(idx, expected=failed)
            raise
        except Exception:
            pass

        if self._closing or idx is None:
            return

        self._schedule_restart(idx, expected=failed)

    async def _maybe_schedule_recycle_after_job(self, worker: ExcelWorkerProcess) -> bool:
        recycle_reason = None
        if self._recycle_jobs > 0 and worker.jobs_run >= self._recycle_jobs:
            recycle_reason = f"jobs_run={worker.jobs_run} >= {self._recycle_jobs}"
        if (
            recycle_reason is None
            and os.name == "nt"
            and self._recycle_private_mb > 0
            and worker.excel_pid is not None
        ):
            private_bytes = await asyncio.to_thread(_process_private_bytes, int(worker.excel_pid))
            if private_bytes is not None:
                private_mb = private_bytes / 1024 / 1024
                if private_mb >= float(self._recycle_private_mb):
                    recycle_reason = f"private_mb={private_mb:.0f} >= {self._recycle_private_mb}"
        if recycle_reason is None:
            return False
        try:
            idx = self._workers.index(worker)
        except ValueError:
            return False
        if self._closing:
            return False
        self._schedule_recycle(idx, expected=worker, reason=recycle_reason)
        return True

    async def _execute_with_worker(
        self,
        worker: ExcelWorkerProcess,
        *,
        run: Callable[[ExcelWorkerProcess], Awaitable[WorkerResult]],
        is_reward: bool,
        timeout_s: float,
    ) -> WorkerResult:
        return_worker: ExcelWorkerProcess | None = worker
        worker_completed = False
        current_worker = worker
        log_prefix = "worker" if is_reward else "recalc worker"

        async def _quiesce_cancelled_worker() -> None:
            nonlocal return_worker
            worker_to_quiesce = return_worker
            if worker_to_quiesce is None:
                return
            return_worker = None
            self._forget_worker_health(worker_to_quiesce)
            try:
                idx = self._workers.index(worker_to_quiesce)
            except ValueError:
                idx = None
            try:
                # Requeue must wait until the cancelled Excel process is dead. If shutdown
                # grace abandons this task first, stale sweep will fail the running job later.
                await _run_cancellation_safe(worker_to_quiesce.shutdown(force=True))
            except (asyncio.CancelledError, Exception):
                pass
            if not self._closing and idx is not None:
                self._schedule_restart(idx, expected=worker_to_quiesce)

        async def _fail_and_replace(
            *,
            err_msg: str,
            cause: BaseException | None,
        ) -> WorkerResult:
            nonlocal return_worker
            return_worker = None
            await self._replace_failed_worker(current_worker)
            if is_reward:
                if cause is None:
                    raise RuntimeError(err_msg)
                raise RuntimeError(err_msg) from cause
            return WorkerResult(ok=False, reward=0.0, msg=err_msg)

        try:
            try:
                result = await run(current_worker)
            except asyncio.CancelledError:
                await _quiesce_cancelled_worker()
                raise
            except asyncio.TimeoutError:
                return await _fail_and_replace(
                    err_msg=f"timeout after {timeout_s:.0f}s",
                    cause=None,
                )
            except WorkerProtocolError as exc:
                err_msg = _public_worker_message(
                    f"worker protocol error: {exc}",
                    fallback="worker protocol error",
                )
                logger.warning(f"[excel_pool] {log_prefix} protocol error: {_format_exception(exc)}")
                return await _fail_and_replace(err_msg=err_msg, cause=exc)
            except WorkerDiedError as exc:
                err_msg = _public_worker_message(
                    f"worker died: {exc}",
                    fallback="worker died",
                )
                logger.warning(f"[excel_pool] {log_prefix} died: {_format_exception(exc)}")
                return await _fail_and_replace(err_msg=err_msg, cause=exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[excel_pool] {log_prefix} error: {_format_exception(exc)}")
                return await _fail_and_replace(err_msg="worker error", cause=exc)

            if not result.ok:
                worker_completed = True
                err_msg = _public_worker_message(result.msg, fallback="worker reported failure")
                if err_msg:
                    logger.warning(f"[excel_pool] {log_prefix} reported failure: {err_msg}")
                if _result_msg_is_fatal_worker_error(result.msg):
                    return_worker = None
                    await self._replace_failed_worker(current_worker)
                else:
                    self._record_worker_healthy_now(current_worker)
                if is_reward:
                    raise RuntimeError(err_msg)
                return WorkerResult(ok=False, reward=0.0, msg=err_msg)
            if await self._maybe_schedule_recycle_after_job(current_worker):
                return_worker = None
            else:
                self._record_worker_healthy_now(current_worker)
            worker_completed = True
            return WorkerResult(
                ok=True,
                reward=float(result.reward),
                msg=_public_worker_message(result.msg, fallback=""),
            )
        except asyncio.CancelledError:
            await _quiesce_cancelled_worker()
            raise
        finally:
            if return_worker is not None:
                try:
                    await self.release(return_worker)
                except asyncio.CancelledError:
                    if not worker_completed:
                        raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[excel_pool] {log_prefix} release failed after job: {_format_exception(exc)}"
                    )
