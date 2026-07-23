from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import TypeVar

from .aio import (
    _await_shielded_ignoring_repeated_cancels,
    _await_worker_communicate_cleanup,
    _run_cancellation_safe,
    _rmtree_with_retries,
    _unlink_with_retries,
)
from .config import (
    _enable_timeout_excel_fallback_kill,
    _get_cleanup_batch_size,
    _get_cleanup_leader_lease_s,
    _get_cleanup_max_batches,
    _get_cleanup_retry_after_s,
    _get_cleanup_retry_batch_share,
    _get_cleanup_retry_max_s,
    _get_health_failure_ttl_s,
    _get_idle_poll_max_s,
    _get_instance_per_worker,
    _get_job_ttl_s,
    _get_max_queue_size,
    _get_max_running_jobs,
    _get_output_root,
    _get_poll_interval_s,
    _get_quarantine_sweep_interval_s,
    _get_sqlite_executor_workers,
    _get_stale_sweep_leader_lease_s,
    _get_windows_excel_diagnostics_dir,
    _get_windows_excel_diagnostics_root,
    _get_worker_timeout_s,
    _keep_files,
)
from .excel_pool import ExcelWorkerPool, ExcelWorkerProcess
from .job_store import JobSnapshot, SqliteJobStore
from .messages import format_exception as _format_exception
from .messages import public_worker_message as _public_worker_message
from .models import JobKind, JobRecord, JobStatus
from .path_safety import (
    _is_expected_reward_proc_file,
    _is_under_root,
    _persisted_job_paths_are_safe,
    _persisted_recalculate_job_paths_are_safe,
    _resolve_for_root_check,
    _unlink_missing_ok,
)
from .platform import Platform
from .windows_process import (
    _kill_excel_pid_from_file,
    _kill_new_excel_processes,
    _kill_subprocess_tree,
    _list_excel_pids,
    _worker_creationflags,
)

logger = logging.getLogger(__name__)
_JOB_TASK_SHUTDOWN_TIMEOUT_S = 5.0
_StoreResultT = TypeVar("_StoreResultT")


class WorkerExitedError(RuntimeError):
    pass


class ControlledWorkerExitedError(WorkerExitedError):
    pass


def _fingerprint_path(path: Path) -> str:
    try:
        text = str(path.resolve(strict=False))
    except OSError:
        text = str(path)
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


def _is_safe_windows_excel_diagnostics_dir(path: Path) -> bool:
    try:
        diagnostics_root = _get_windows_excel_diagnostics_root().resolve(strict=False)
        target = path.resolve(strict=False)
    except OSError:
        return False
    return target != diagnostics_root and target.is_relative_to(diagnostics_root)


def _is_explicit_windows_excel_diagnostics_cleanup_enabled() -> bool:
    return _get_windows_excel_diagnostics_dir() is not None


def _delete_dir_contents(path: Path) -> None:
    def _onerror(func, p, _: object) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    try:
        if not path.exists() or not path.is_dir():
            return
        for child in path.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, onerror=_onerror)
                else:
                    try:
                        child.unlink(missing_ok=True)
                    except OSError:
                        os.chmod(child, stat.S_IWRITE)
                        child.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _parse_worker_stdout(stdout_text: str, *, kind: JobKind) -> dict[str, object] | None:
    expected_keys = {"ok", "reward", "msg"} if kind is JobKind.REWARD else {"ok", "msg"}
    fallback_payload: dict[str, object] | None = None
    for line in reversed(stdout_text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            continue
        payload_keys = set(payload)
        if not expected_keys.issubset(payload_keys):
            if (
                kind is JobKind.REWARD
                and {"ok", "msg"}.issubset(payload_keys)
                and "reward" not in payload
                and fallback_payload is None
            ):
                fallback_payload = payload
            continue
        if kind is JobKind.REWARD:
            return payload
        if kind is JobKind.RECALCULATE and "msg" in payload:
            return payload
    return fallback_payload


def _parse_worker_reward(payload: dict[str, object]) -> float:
    if "reward" not in payload:
        raise RuntimeError("missing worker reward")
    if isinstance(payload["reward"], bool):
        raise RuntimeError("invalid worker reward")
    try:
        reward = float(payload["reward"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid worker reward") from exc
    if not math.isfinite(reward):
        raise RuntimeError("invalid worker reward")
    return reward


def _start_worker_subprocess(*, cmd: list[str]) -> subprocess.Popen[bytes]:
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        flags = _worker_creationflags()
        if flags:
            popen_kwargs["creationflags"] = flags
    else:
        popen_kwargs["start_new_session"] = True

    return subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603,S607 - controlled local command



def _terminal_stderr_text(stderr_text: str) -> str:
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    if lines:
        return lines[-1]
    return stderr_text.strip()


def _is_controlled_worker_exit_stderr(stderr_text: str) -> bool:
    return stderr_text.lstrip().lower().startswith("[worker] fatal recalc failed:")


async def _run_worker_subprocess(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
    proc = _start_worker_subprocess(cmd=cmd)
    communicate_task = asyncio.create_task(
        asyncio.to_thread(proc.communicate, timeout=float(timeout_s))
    )
    try:
        stdout, stderr = await asyncio.shield(communicate_task)
        returncode = proc.returncode
        if returncode is not None and int(returncode) != 0:
            msg = f"worker exited with returncode={int(returncode)}"
            stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()
            terminal_stderr = _terminal_stderr_text(stderr_text)
            stderr_msg = _public_worker_message(terminal_stderr, fallback="")
            if stderr_msg:
                msg = f"{msg}; stderr: {stderr_msg}"
            if _is_controlled_worker_exit_stderr(terminal_stderr):
                raise ControlledWorkerExitedError(msg)
            raise WorkerExitedError(msg)
        return False, stdout or b"", stderr or b""
    except subprocess.TimeoutExpired:
        _kill_subprocess_tree(proc)
        try:
            stdout, stderr = await asyncio.to_thread(proc.communicate, timeout=5)
        except Exception:
            stdout, stderr = b"", b""
        return True, stdout or b"", stderr or b""
    except asyncio.CancelledError:
        _kill_subprocess_tree(proc)
        await _await_worker_communicate_cleanup(communicate_task, timeout_s=5)
        raise
    except WorkerExitedError:
        raise
    except Exception:
        _kill_subprocess_tree(proc)
        try:
            await asyncio.wait_for(asyncio.shield(communicate_task), timeout=5)
        except Exception:
            pass
        raise

class RewardJobManager:
    def __init__(self, *, store: SqliteJobStore, platform: Platform) -> None:
        self._store = store
        self._platform = platform
        self._instance_id = uuid.uuid4().hex
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}:{self._instance_id}"
        self._db_fingerprint = _fingerprint_path(store.db_path)
        max_running_jobs = _get_max_running_jobs()
        self._poll_interval_s = _get_poll_interval_s()
        self._idle_poll_max_s = _get_idle_poll_max_s(self._poll_interval_s)
        self._instance_per_worker = _get_instance_per_worker()
        if (
            platform is Platform.WINDOWS
            and self._instance_per_worker <= 0
            and max_running_jobs is None
        ):
            max_running_jobs = 1
        self._configured_max_running_jobs = max_running_jobs
        self._max_running_jobs = self._configured_max_running_jobs
        self._sqlite_executor_workers = _get_sqlite_executor_workers()
        self._store_executor: ThreadPoolExecutor | None = None
        self._store_executor_closing = False
        self._store_calls_allowed = True
        self._excel_pool: ExcelWorkerPool | None = None
        self._run_sem: asyncio.Semaphore | None = None
        self._job_tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._submit_wake = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._windows_excel_diagnostics_cleanup_task: asyncio.Task | None = None
        self._deferred_store_close_task: asyncio.Task | None = None
        self._excel_pool_start_failed = False
        self._job_task_failures = 0
        self._last_job_task_failure_s: float | None = None
        self._last_job_task_error = ""
        self._background_loop_failures = 0
        self._last_background_loop_failure_s: float | None = None
        self._last_background_loop_error = ""

    def _ensure_store_executor(self) -> ThreadPoolExecutor:
        if self._store_executor_closing:
            raise RuntimeError("job store executor is shutting down")
        if not self._store_calls_allowed:
            raise RuntimeError("job store executor is shut down")
        executor = self._store_executor
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=self._sqlite_executor_workers,
                thread_name_prefix=f"reward-sqlite-{self._instance_id[:8]}",
            )
            self._store_executor = executor
        return executor

    async def _run_store(
        self,
        operation: Callable[..., _StoreResultT],
        /,
        *args: object,
        **kwargs: object,
    ) -> _StoreResultT:
        executor = self._ensure_store_executor()
        return await asyncio.get_running_loop().run_in_executor(
            executor,
            partial(operation, *args, **kwargs),
        )

    async def _close_store_and_shutdown_executor(self) -> None:
        executor = self._store_executor
        self._store_executor_closing = True
        self._store_calls_allowed = False
        try:
            if executor is not None:
                await asyncio.to_thread(
                    executor.shutdown,
                    wait=True,
                    cancel_futures=False,
                )
            if self._store_executor is executor:
                self._store_executor = None
            await asyncio.to_thread(self._store.close)
        finally:
            if self._store_executor is executor:
                self._store_executor = None
            self._store_executor_closing = False

    async def _cleanup_after_failed_start(self) -> None:
        try:
            await self.shutdown()
        finally:
            if self._store_executor is not None or self._store_calls_allowed:
                await self._close_store_and_shutdown_executor()

    async def start(self) -> None:
        for task in (
            self._worker_task,
            self._cleanup_task,
            self._windows_excel_diagnostics_cleanup_task,
        ):
            if task is not None and not task.done():
                raise RuntimeError("RewardJobManager is already started")
        if self._job_tasks:
            raise RuntimeError("RewardJobManager has active job tasks")
        await self._cancel_deferred_store_close()
        self._stop = asyncio.Event()
        self._submit_wake = asyncio.Event()
        self._worker_task = None
        self._cleanup_task = None
        self._windows_excel_diagnostics_cleanup_task = None
        self._max_running_jobs = self._configured_max_running_jobs
        self._excel_pool_start_failed = False
        self._job_task_failures = 0
        self._last_job_task_failure_s = None
        self._last_job_task_error = ""
        self._background_loop_failures = 0
        self._last_background_loop_failure_s = None
        self._last_background_loop_error = ""
        self._store_calls_allowed = True

        try:
            await self._run_store(self._store.init)
            await self._sweep_stale_running_jobs_once(pass_now_s=time.time())
            if (
                self._platform is Platform.WINDOWS
                and os.name == "nt"
                and self._instance_per_worker > 0
            ):
                self._excel_pool = ExcelWorkerPool(
                    size=self._excel_pool_start_size(),
                    platform=self._platform.value,
                )
                try:
                    await self._excel_pool.start()
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "[main] Excel pool start failed; falling back to per-job workers: "
                        f"{_format_exception(exc)}"
                    )
                    try:
                        await self._excel_pool.shutdown(force=True)
                    except Exception:
                        pass
                    self._excel_pool = None
                    self._excel_pool_start_failed = True
                    if self._max_running_jobs is None:
                        self._max_running_jobs = 1

            concurrency = self._effective_worker_concurrency()
            self._run_sem = asyncio.Semaphore(concurrency)
            self._worker_task = asyncio.create_task(self._worker_loop())
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            if (
                self._platform is Platform.WINDOWS
                and os.name == "nt"
                and _is_explicit_windows_excel_diagnostics_cleanup_enabled()
            ):
                self._windows_excel_diagnostics_cleanup_task = asyncio.create_task(
                    self._windows_excel_diagnostics_cleanup_loop()
                )
        except BaseException:
            try:
                await _run_cancellation_safe(self._cleanup_after_failed_start())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[main] failed-start cleanup failed: {_format_exception(exc)}")
            raise

    async def shutdown(self) -> None:
        await _run_cancellation_safe(self._shutdown_impl())

    async def _shutdown_impl(self) -> None:
        await self._cancel_deferred_store_close()
        self._stop.set()
        still_pending: set[asyncio.Task] = set()
        for task in (self._worker_task, self._cleanup_task, self._windows_excel_diagnostics_cleanup_task):
            if task is None:
                continue
            task.cancel()
        for task in (self._worker_task, self._cleanup_task, self._windows_excel_diagnostics_cleanup_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[main] background task failed before shutdown completed: {_format_exception(exc)}")
        if self._job_tasks:
            tasks = list(self._job_tasks)
            _, pending = await asyncio.wait(tasks, timeout=_JOB_TASK_SHUTDOWN_TIMEOUT_S)
            for task in pending:
                task.cancel()
            if pending:
                _, still_pending = await asyncio.wait(
                    pending,
                    timeout=_JOB_TASK_SHUTDOWN_TIMEOUT_S,
                )
                for task in still_pending:
                    self._record_job_task_error(
                        "job task shutdown timed out",
                        RuntimeError("job task did not stop after cancellation"),
                    )
        pool = self._excel_pool
        try:
            if pool is not None:
                await pool.shutdown(force=True)
        finally:
            if self._excel_pool is pool:
                self._excel_pool = None
            if still_pending:
                self._deferred_store_close_task = asyncio.create_task(
                    self._deferred_close_store_after_jobs(set(still_pending))
                )
            else:
                await _run_cancellation_safe(self._close_store_and_shutdown_executor())

    async def _cancel_deferred_store_close(self) -> None:
        task = self._deferred_store_close_task
        if task is None:
            return
        self._deferred_store_close_task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[main] deferred store close failed: {_format_exception(exc)}")

    async def _deferred_close_store_after_jobs(self, tasks: set[asyncio.Task]) -> None:
        try:
            await asyncio.wait(tasks)
            await _run_cancellation_safe(self._close_store_and_shutdown_executor())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[main] deferred store close failed: {_format_exception(exc)}")

    def _effective_worker_concurrency(self) -> int:
        if self._excel_pool is not None:
            pool_size = max(1, int(self._excel_pool.size))
            if self._max_running_jobs is not None:
                return min(pool_size, max(1, int(self._max_running_jobs)))
            return pool_size
        return max(1, int(self._max_running_jobs or 1))

    def _excel_pool_start_size(self) -> int:
        pool_size = max(1, int(self._instance_per_worker))
        if self._max_running_jobs is not None:
            return min(pool_size, max(1, int(self._max_running_jobs)))
        return pool_size

    async def submit(self, job: JobRecord) -> bool:
        accepted = await self._run_store(
            self._store.enqueue,
            job,
            max_queue_size=_get_max_queue_size(),
        )
        if accepted:
            self._submit_wake.set()
        return accepted

    def _record_job_task_error(self, context: str, exc: BaseException) -> None:
        self._job_task_failures += 1
        self._last_job_task_failure_s = time.time()
        self._last_job_task_error = _public_worker_message(
            f"{context}: {_format_exception(exc)}",
            fallback=context,
        )
        logger.warning(f"[main] {context}: {_format_exception(exc)}")

    def _record_background_loop_error(self, context: str, exc: BaseException) -> None:
        self._background_loop_failures += 1
        self._last_background_loop_failure_s = time.time()
        self._last_background_loop_error = _public_worker_message(
            f"{context}: {_format_exception(exc)}",
            fallback=context,
        )
        logger.warning(f"[main] {context}: {_format_exception(exc)}")

    def _on_job_task_done(self, task: asyncio.Task) -> None:
        self._job_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            self._record_job_task_error("job task failed", exc)

    async def _finish_job(
        self,
        *,
        job_id: str,
        status: JobStatus,
        reward: float,
        msg: str,
    ) -> bool:
        try:
            finish_task = asyncio.create_task(
                self._run_store(
                    self._store.finish,
                    job_id=job_id,
                    status=status,
                    reward=reward,
                    msg=msg,
                    worker_id=self._worker_id,
                )
            )
            try:
                updated = await asyncio.shield(finish_task)
            except asyncio.CancelledError:
                updated = await _await_shielded_ignoring_repeated_cancels(finish_task)
            if not updated:
                self._record_job_task_error(
                    "job finish failed",
                    RuntimeError(f"job {job_id} was not running"),
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self._record_job_task_error("job finish failed", exc)
            return False

    async def has_queue_capacity(self) -> bool:
        return await self._run_store(
            self._store.has_queue_capacity,
            max_queue_size=_get_max_queue_size(),
        )

    async def get_snapshot(self, job_id: str) -> JobSnapshot | None:
        return await self._run_store(self._store.get_snapshot, job_id)

    async def mark_result_expired(self, *, job_id: str, kind: JobKind) -> bool:
        return await self._run_store(self._store.mark_result_expired, job_id=job_id, kind=kind)

    async def mark_result_error(self, *, job_id: str, kind: JobKind, msg: str) -> bool:
        return await self._run_store(
            self._store.mark_result_error,
            job_id=job_id,
            kind=kind,
            msg=msg,
        )

    async def _recalc_job(
        self,
        job: JobRecord,
        *,
        excel_worker: ExcelWorkerProcess | None,
        use_excel_pool: bool,
    ) -> str:
        if use_excel_pool and self._excel_pool is not None and excel_worker is not None:
            ok, msg = await self._excel_pool.recalc_file_with_worker(
                excel_worker,
                job_id=job.job_id,
                proc_file=job.proc_file,
                timeout_s=_get_worker_timeout_s(),
            )
            if not ok:
                raise RuntimeError(msg or "recalc failed")
            return msg

        ok, msg = await _recalc_file_via_worker(proc_file=job.proc_file, platform=self._platform)
        if not ok:
            raise RuntimeError(msg or "recalc failed")
        return msg

    async def _release_excel_worker_after_claim(
        self,
        excel_worker: ExcelWorkerProcess | None,
        *,
        context: str,
    ) -> bool:
        if excel_worker is None or self._excel_pool is None:
            return True
        try:
            await self._excel_pool.release(excel_worker)
            return True
        except asyncio.CancelledError:
            self._record_background_loop_error(
                f"{context} worker release cancelled",
                RuntimeError("pooled worker release cancelled"),
            )
            return False
        except Exception as exc:  # noqa: BLE001
            self._record_background_loop_error(f"{context} worker release failed", exc)
            return True

    async def _requeue_claimed_cancellation_safe(
        self,
        job_id: str,
        *,
        context: str,
        record_not_requeued: bool,
    ) -> bool:
        try:
            requeue_task = asyncio.create_task(
                self._run_store(
                    self._store.requeue_claimed,
                    job_id=job_id,
                    worker_id=self._worker_id,
                )
            )
            try:
                requeued = await asyncio.shield(requeue_task)
            except asyncio.CancelledError:
                requeued = await _await_shielded_ignoring_repeated_cancels(requeue_task)
        except Exception as exc:  # noqa: BLE001
            self._record_job_task_error(context, exc)
            return False
        if not requeued and record_not_requeued:
            self._record_job_task_error(
                context,
                RuntimeError(f"job {job_id} was not requeued"),
            )
        return bool(requeued)

    async def _worker_loop(self) -> None:
        idle_sleep_s = self._poll_interval_s
        while not self._stop.is_set():
            if self._run_sem is None:
                raise RuntimeError("RewardJobManager is not started")
            await self._run_sem.acquire()

            excel_worker: ExcelWorkerProcess | None = None
            use_excel_pool = False
            claim_task: asyncio.Task[JobRecord | None] | None = None
            try:
                if not await _run_cancellation_safe(
                    self._run_store(
                        self._store.has_claimable_jobs,
                        self._max_running_jobs,
                    )
                ):
                    self._run_sem.release()
                    try:
                        await asyncio.wait_for(self._submit_wake.wait(), timeout=idle_sleep_s)
                    except asyncio.TimeoutError:
                        idle_sleep_s = min(idle_sleep_s * 1.5, self._idle_poll_max_s)
                    else:
                        self._submit_wake.clear()
                        idle_sleep_s = self._poll_interval_s
                    continue

                if self._excel_pool is not None:
                    excel_worker = await self._excel_pool.acquire()
                    use_excel_pool = True

                claim_task = asyncio.create_task(
                    self._run_store(
                        self._store.claim_next,
                        worker_id=self._worker_id,
                        max_running_jobs=self._max_running_jobs,
                    )
                )
                job = await asyncio.shield(claim_task)
            except asyncio.CancelledError:
                try:
                    if claim_task is not None:
                        try:
                            claimed_after_cancel = await _await_shielded_ignoring_repeated_cancels(claim_task)
                        except Exception as exc:  # noqa: BLE001
                            self._record_job_task_error("cancelled claim failed", exc)
                        else:
                            if claimed_after_cancel is not None:
                                await _run_cancellation_safe(
                                    self._requeue_claimed_cancellation_safe(
                                        claimed_after_cancel.job_id,
                                        context="cancelled claim requeue failed",
                                        record_not_requeued=True,
                                    )
                                )
                finally:
                    try:
                        await self._release_excel_worker_after_claim(
                            excel_worker,
                            context="cancelled claim",
                        )
                    finally:
                        self._run_sem.release()
                raise
            except Exception as exc:  # noqa: BLE001
                self._record_background_loop_error("job acquisition failed", exc)
                release_ok = True
                try:
                    release_ok = await self._release_excel_worker_after_claim(
                        excel_worker,
                        context="failed claim",
                    )
                finally:
                    self._run_sem.release()
                if not release_ok:
                    raise asyncio.CancelledError
                await asyncio.sleep(1.0)
                continue
            if job is None:
                release_ok = True
                try:
                    release_ok = await self._release_excel_worker_after_claim(
                        excel_worker,
                        context="empty claim",
                    )
                finally:
                    self._run_sem.release()
                if not release_ok:
                    raise asyncio.CancelledError
                try:
                    await asyncio.wait_for(self._submit_wake.wait(), timeout=idle_sleep_s)
                except asyncio.TimeoutError:
                    idle_sleep_s = min(idle_sleep_s * 1.5, self._idle_poll_max_s)
                else:
                    self._submit_wake.clear()
                    idle_sleep_s = self._poll_interval_s
                continue

            idle_sleep_s = self._poll_interval_s
            task = asyncio.create_task(
                self._run_job(job, excel_worker=excel_worker, use_excel_pool=use_excel_pool)
            )
            self._job_tasks.add(task)
            task.add_done_callback(self._on_job_task_done)

    async def _run_job(
        self,
        job: JobRecord,
        *,
        excel_worker: ExcelWorkerProcess | None,
        use_excel_pool: bool,
    ) -> None:
        terminal_finish_ok = False
        worker_delegated = False
        try:
            paths_are_safe = await asyncio.to_thread(_persisted_job_paths_are_safe, job)
            if not paths_are_safe:
                terminal_finish_ok = await self._finish_job(
                    job_id=job.job_id,
                    status=JobStatus.ERROR,
                    reward=0.0,
                    msg="invalid persisted job path",
                )
                return
            if job.kind is JobKind.REWARD:
                worker_delegated = True
                reward, msg = await self._compute_reward(
                    job, excel_worker=excel_worker, use_excel_pool=use_excel_pool
                )
                terminal_finish_ok = await self._finish_job(
                    job_id=job.job_id,
                    status=JobStatus.DONE,
                    reward=reward,
                    msg=msg,
                )
            elif job.kind is JobKind.RECALCULATE:
                worker_delegated = True
                msg = await self._recalc_job(job, excel_worker=excel_worker, use_excel_pool=use_excel_pool)
                terminal_finish_ok = await self._finish_job(
                    job_id=job.job_id,
                    status=JobStatus.DONE,
                    reward=0.0,
                    msg=msg,
                )
            else:
                raise RuntimeError(f"unknown job kind: {job.kind}")
        except asyncio.CancelledError:
            requeued = False
            requeued = await self._requeue_claimed_cancellation_safe(
                job.job_id,
                context="job cancel requeue failed",
                record_not_requeued=False,
            )
            if not requeued:
                terminal_finish_ok = await self._finish_job(
                    job_id=job.job_id,
                    status=JobStatus.ERROR,
                    reward=0.0,
                    msg="job cancelled",
                )
            raise
        except Exception as exc:  # noqa: BLE001
            terminal_finish_ok = await self._finish_job(
                job_id=job.job_id,
                status=JobStatus.ERROR,
                reward=0.0,
                msg=_public_worker_message(
                    f"worker exception: {_format_exception(exc)}",
                    fallback="worker exception",
                ),
            )
        finally:
            if use_excel_pool and excel_worker is not None and not worker_delegated:
                await self._release_excel_worker_after_claim(excel_worker, context="undelegated job")
            if self._run_sem is not None:
                self._run_sem.release()
                self._submit_wake.set()
            if job.kind is JobKind.REWARD and terminal_finish_ok and not _keep_files():
                under_output_root = _is_under_root(job.proc_file, _get_output_root())
                expected_output_path = _is_expected_reward_proc_file(
                    job_id=job.job_id,
                    gt_file=job.gt_file,
                    proc_file=job.proc_file,
                )
                if not under_output_root:
                    logger.warning(f"[_run_job] refusing to delete out-of-root reward upload for job {job.job_id}")
                elif not expected_output_path:
                    logger.warning(f"[_run_job] refusing to delete unexpected reward upload for job {job.job_id}")
                else:
                    delays_s = (0.0, 0.25, 1.0) if os.name == "nt" else (0.0,)
                    for delay_s in delays_s:
                        if delay_s:
                            await asyncio.sleep(delay_s)
                        try:
                            await asyncio.to_thread(job.proc_file.unlink, missing_ok=True)
                            break
                        except OSError:
                            continue

    async def _compute_reward(
        self,
        job: JobRecord,
        *,
        excel_worker: ExcelWorkerProcess | None,
        use_excel_pool: bool,
    ) -> tuple[float, str]:
        if use_excel_pool and self._excel_pool is not None:
            return await self._excel_pool.run_job_with_worker(
                excel_worker,
                job_id=job.job_id,
                gt_file=job.gt_file,
                proc_file=job.proc_file,
                answer_position=job.answer_position,
                timeout_s=_get_worker_timeout_s(),
            )
        return await _compute_reward_via_worker(
            gt_file=job.gt_file,
            proc_file=job.proc_file,
            answer_position=job.answer_position,
            platform=self._platform,
        )

    async def _quarantine_invalid_jobs_once(
        self,
        *,
        pass_now_s: float,
        interval_s: float,
    ) -> bool:
        if interval_s <= 0:
            return False
        owns_quarantine_lease = await _run_cancellation_safe(
            self._run_store(
                self._store.try_acquire_maintenance_lease,
                name="quarantine",
                owner_id=self._worker_id,
                lease_s=interval_s,
                now_s=pass_now_s,
                allow_owner_renewal=False,
            )
        )
        if not owns_quarantine_lease:
            return False
        await _run_cancellation_safe(self._run_store(self._store.quarantine_invalid_jobs))
        return True

    async def _sweep_stale_running_jobs_once(self, *, pass_now_s: float) -> bool:
        quarantine_interval_s = _get_quarantine_sweep_interval_s()
        if quarantine_interval_s > 0:
            await self._quarantine_invalid_jobs_once(
                pass_now_s=pass_now_s,
                interval_s=quarantine_interval_s,
            )
        stale_lease_s = _get_stale_sweep_leader_lease_s()
        owns_stale_sweep_lease = await _run_cancellation_safe(
            self._run_store(
                self._store.try_acquire_maintenance_lease,
                name="stale_sweep",
                owner_id=self._worker_id,
                lease_s=stale_lease_s,
                now_s=pass_now_s,
            )
        )
        if not owns_stale_sweep_lease:
            return False
        if quarantine_interval_s <= 0:
            await _run_cancellation_safe(self._run_store(self._store.quarantine_invalid_jobs))
        await _run_cancellation_safe(
            self._run_store(
                self._store.mark_stale_running_as_error,
                older_than_s=_get_worker_timeout_s() + 60.0,
                msg="stale running job (worker crashed?)",
            )
        )
        return True

    async def _cleanup_finished_jobs_once(self, *, pass_now_s: float) -> None:
        await self._sweep_stale_running_jobs_once(pass_now_s=pass_now_s)

        lease_s = _get_cleanup_leader_lease_s()
        owns_cleanup_lease = await _run_cancellation_safe(
            self._run_store(
                self._store.try_acquire_maintenance_lease,
                name="cleanup",
                owner_id=self._worker_id,
                lease_s=lease_s,
                now_s=pass_now_s,
            )
        )
        if not owns_cleanup_lease:
            return

        ttl_s = _get_job_ttl_s()
        cutoff_s = pass_now_s - ttl_s
        batch_size = _get_cleanup_batch_size()
        retry_batch_share = _get_cleanup_retry_batch_share()
        max_batches = _get_cleanup_max_batches()

        if _keep_files():
            for _ in range(max_batches):
                still_leader = await _run_cancellation_safe(
                    self._run_store(
                        self._store.try_acquire_maintenance_lease,
                        name="cleanup",
                        owner_id=self._worker_id,
                        lease_s=lease_s,
                        now_s=time.time(),
                    )
                )
                if not still_leader:
                    break

                jobs = await _run_cancellation_safe(
                    self._run_store(
                        self._store.list_cleanup_batch,
                        cutoff_s=cutoff_s,
                        batch_size=batch_size,
                        retry_batch_share=retry_batch_share,
                        now_s=time.time(),
                    )
                )
                if not jobs:
                    break

                await _run_cancellation_safe(
                    self._run_store(
                        self._store.delete_jobs,
                        job_ids=[job.job_id for job in jobs],
                    )
                )
                if len(jobs) < batch_size:
                    break
            return

        output_root = _get_output_root()
        output_root_resolved = _resolve_for_root_check(output_root)
        retry_after_s = _get_cleanup_retry_after_s()
        retry_max_s = _get_cleanup_retry_max_s()
        delete_delays_s = (0.0, 0.25, 1.0) if os.name == "nt" else (0.0,)
        for _ in range(max_batches):
            still_leader = await _run_cancellation_safe(
                self._run_store(
                    self._store.try_acquire_maintenance_lease,
                    name="cleanup",
                    owner_id=self._worker_id,
                    lease_s=lease_s,
                    now_s=time.time(),
                )
            )
            if not still_leader:
                break

            jobs = await _run_cancellation_safe(
                self._run_store(
                    self._store.list_cleanup_batch,
                    cutoff_s=cutoff_s,
                    batch_size=batch_size,
                    retry_batch_share=retry_batch_share,
                    now_s=time.time(),
                )
            )
            if not jobs:
                break

            deletable_job_ids: list[str] = []
            failed_job_ids: list[str] = []

            for cleanup_job in jobs:
                job_id = cleanup_job.job_id
                kind = cleanup_job.kind
                proc_file = cleanup_job.proc_file

                def _mark_cleanup_path_failure(message: str) -> None:
                    logger.warning(message)
                    if cleanup_job.quarantined_invalid:
                        deletable_job_ids.append(job_id)
                    else:
                        failed_job_ids.append(job_id)

                # Empty persisted proc_file values are returned as Path("."); there is no
                # safe file to unlink, but the terminal DB row should not live forever.
                if str(proc_file) == ".":
                    deletable_job_ids.append(job_id)
                    continue

                if kind is JobKind.REWARD:
                    try:
                        proc_file_resolved = proc_file.resolve(strict=False)
                    except (OSError, ValueError):
                        _mark_cleanup_path_failure(
                            f"[_cleanup_loop] refusing to delete malformed reward file for job {job_id!r}",
                        )
                        continue
                    if not _is_expected_reward_proc_file(
                        job_id=job_id,
                        gt_file=cleanup_job.gt_file,
                        proc_file=proc_file,
                    ):
                        _mark_cleanup_path_failure(
                            f"[_cleanup_loop] refusing to delete unexpected reward file for job {job_id!r}",
                        )
                        continue
                    if not proc_file_resolved.is_relative_to(output_root_resolved):
                        _mark_cleanup_path_failure(
                            f"[_cleanup_loop] refusing to delete out-of-root reward file for job {job_id}",
                        )
                        continue
                    if not await _unlink_with_retries(proc_file, delete_delays_s):
                        failed_job_ids.append(job_id)
                        continue
                    deletable_job_ids.append(job_id)
                    continue

                if not _persisted_recalculate_job_paths_are_safe(cleanup_job):
                    _mark_cleanup_path_failure(
                        f"[_cleanup_loop] refusing to delete unexpected recalculate file for job {job_id!r}",
                    )
                    continue

                parent = proc_file.parent
                if not await _rmtree_with_retries(parent, delete_delays_s):
                    failed_job_ids.append(job_id)
                    continue
                deletable_job_ids.append(job_id)

            if deletable_job_ids:
                await _run_cancellation_safe(
                    self._run_store(self._store.delete_jobs, job_ids=deletable_job_ids)
                )
            if failed_job_ids:
                await _run_cancellation_safe(
                    self._run_store(
                        self._store.mark_cleanup_failed,
                        job_ids=failed_job_ids,
                        retry_after_s=retry_after_s,
                        retry_max_s=retry_max_s,
                    )
                )
            if len(jobs) < batch_size:
                break

    async def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            try:
                await self._cleanup_finished_jobs_once(pass_now_s=time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._record_background_loop_error("cleanup iteration failed", exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def _windows_excel_diagnostics_cleanup_loop(self) -> None:
        while not self._stop.is_set():
            diagnostics_dir = _get_windows_excel_diagnostics_dir()
            if diagnostics_dir is None:
                return
            if not _is_safe_windows_excel_diagnostics_dir(diagnostics_dir):
                logger.error("[main] unsafe Windows Excel diagnostics cleanup directory")
                raise RuntimeError("unsafe Windows Excel diagnostics cleanup directory")
            await asyncio.to_thread(_delete_dir_contents, diagnostics_dir)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600.0)
            except asyncio.TimeoutError:
                continue

    def _background_task_statuses(self) -> tuple[dict[str, dict[str, str]], bool]:
        tasks: list[tuple[str, asyncio.Task | None]] = [
            ("worker_loop", self._worker_task),
            ("cleanup_loop", self._cleanup_task),
        ]
        if self._windows_excel_diagnostics_cleanup_task is not None:
            tasks.append(
                ("windows_excel_diagnostics_cleanup", self._windows_excel_diagnostics_cleanup_task)
            )

        statuses: dict[str, dict[str, str]] = {}
        healthy = True
        for name, task in tasks:
            if task is None:
                statuses[name] = {"state": "not_started"}
                healthy = False
                continue
            if task.cancelled():
                statuses[name] = {"state": "cancelled"}
                healthy = False
                continue
            if not task.done():
                statuses[name] = {"state": "running"}
                continue

            try:
                exc = task.exception()
            except asyncio.CancelledError:
                statuses[name] = {"state": "cancelled"}
                healthy = False
                continue

            if exc is None:
                statuses[name] = {"state": "finished"}
            else:
                statuses[name] = {"state": "failed", "error": type(exc).__name__}
            healthy = False

        return statuses, healthy

    @staticmethod
    def _failure_window_healthy(
        *,
        failures: int,
        last_failure_s: float | None,
        ttl_s: float,
        now_s: float,
    ) -> bool:
        if failures <= 0:
            return True
        if ttl_s <= 0:
            return False
        if last_failure_s is None:
            return False
        return now_s - last_failure_s >= ttl_s

    async def stats(self) -> dict[str, object]:
        counts = await self._run_store(self._store.stats)
        background_tasks, background_tasks_running = self._background_task_statuses()
        health_failure_ttl_s = _get_health_failure_ttl_s()
        health_now_s = time.time()
        background_loop_healthy = self._failure_window_healthy(
            failures=self._background_loop_failures,
            last_failure_s=self._last_background_loop_failure_s,
            ttl_s=health_failure_ttl_s,
            now_s=health_now_s,
        )
        background_tasks_healthy = background_tasks_running and background_loop_healthy
        if self._excel_pool is None:
            excel_pool_healthy = not self._excel_pool_start_failed
            excel_pool: dict[str, object] = {
                "enabled": False,
                "mode": "per_job",
                "configured_instances": self._instance_per_worker,
                "slots": 0,
                "active_instances": 0,
                "alive_instances": 0,
                "available_instances": 0,
                "restart_pending": 0,
                "recycle_pending": 0,
                "startup_failed": self._excel_pool_start_failed,
            }
            concurrency = self._effective_worker_concurrency()
        else:
            excel_pool = await self._excel_pool.status()
            excel_pool["startup_failed"] = False
            excel_pool_healthy = int(excel_pool.get("alive_instances") or 0) > 0
            concurrency = self._effective_worker_concurrency()
        job_tasks_healthy = self._failure_window_healthy(
            failures=self._job_task_failures,
            last_failure_s=self._last_job_task_failure_s,
            ttl_s=health_failure_ttl_s,
            now_s=health_now_s,
        )
        job_tasks: dict[str, object] = {
            "active": len(self._job_tasks),
            "failures": self._job_task_failures,
        }
        if self._last_job_task_error:
            job_tasks["last_error"] = self._last_job_task_error
        background_loop_errors: dict[str, object] = {
            "failures": self._background_loop_failures,
        }
        if self._last_background_loop_error:
            background_loop_errors["last_error"] = self._last_background_loop_error
        ready = background_tasks_healthy and excel_pool_healthy and job_tasks_healthy
        return {
            **counts,
            "max_queue_size": _get_max_queue_size(),
            "platform": self._platform.value,
            "instance_id": self._instance_id,
            "db_fingerprint": self._db_fingerprint,
            "max_running_jobs": self._max_running_jobs,
            "instance_per_worker": self._instance_per_worker,
            "concurrency": concurrency,
            "excel_pool": excel_pool,
            "excel_pool_healthy": excel_pool_healthy,
            "background_tasks": background_tasks,
            "background_tasks_healthy": background_tasks_healthy,
            "background_loop_errors": background_loop_errors,
            "job_tasks": job_tasks,
            "job_tasks_healthy": job_tasks_healthy,
            "ready": ready,
        }

async def _cleanup_worker_excel_pid_file(
    *,
    platform: Platform,
    pid_file: Path,
    use_fallback_excel_kill: bool,
    baseline_excel_pids: set[int] | None,
) -> tuple[bool, int]:
    killed_specific = await asyncio.to_thread(
        _kill_excel_pid_from_file,
        platform=platform,
        pid_file=pid_file,
    )
    killed_fallback = 0
    if not killed_specific and use_fallback_excel_kill:
        killed_fallback = await asyncio.to_thread(
            _kill_new_excel_processes,
            platform=platform,
            baseline_pids=baseline_excel_pids,
        )
    return killed_specific, killed_fallback


async def _cleanup_worker_excel_pid_file_cancellation_safe(
    *,
    platform: Platform,
    pid_file: Path,
    use_fallback_excel_kill: bool,
    baseline_excel_pids: set[int] | None,
    context: str,
) -> tuple[bool, int]:
    task = asyncio.create_task(
        _cleanup_worker_excel_pid_file(
            platform=platform,
            pid_file=pid_file,
            use_fallback_excel_kill=use_fallback_excel_kill,
            baseline_excel_pids=baseline_excel_pids,
        )
    )
    try:
        killed_specific, killed_fallback = await asyncio.shield(task)
    except asyncio.CancelledError:
        killed_specific, killed_fallback = await _await_shielded_ignoring_repeated_cancels(task)
        if not killed_specific:
            logger.warning(f"[main] {context} cancellation without attributed Excel cleanup "
                f"(fallback_killed={killed_fallback})")
        raise
    if not killed_specific:
        logger.warning(f"[main] {context} cancellation without attributed Excel cleanup "
            f"(fallback_killed={killed_fallback})")
    return killed_specific, killed_fallback


async def _run_worker_subprocess_job(
    kind: JobKind,
    *,
    proc_file: Path,
    platform: Platform,
    gt_file: Path | None = None,
    answer_position: str | None = None,
) -> dict[str, object]:
    excel_pid_file = Path(tempfile.gettempdir()) / f"async_reward_api_excel_pid_{uuid.uuid4().hex}.txt"
    use_fallback_excel_kill = _enable_timeout_excel_fallback_kill()
    baseline_excel_pids: set[int] | None = set()
    if use_fallback_excel_kill:
        baseline_excel_pids = await asyncio.to_thread(_list_excel_pids, platform)
    cleanup_fallback_excel_kill = use_fallback_excel_kill
    is_reward = kind is JobKind.REWARD
    log_label = "_compute_reward_via_worker" if is_reward else "_recalc_file_via_worker"
    context = "reward worker" if is_reward else "recalc worker"
    cmd = [
        sys.executable,
        "-m",
        "async_reward_api.worker",
        "--platform",
        platform.value,
    ]
    if is_reward:
        if gt_file is None or answer_position is None:
            raise ValueError("reward worker jobs require gt_file and answer_position")
        cmd.extend(
            [
                "--gt-file",
                str(gt_file),
                "--proc-file",
                str(proc_file),
                "--answer-position",
                answer_position,
            ]
        )
    else:
        cmd.extend(
            [
                "--proc-file",
                str(proc_file),
                "--recalc-only",
            ]
        )
    cmd.extend(
        [
            "--excel-pid-file",
            str(excel_pid_file),
        ]
    )

    timeout_s = _get_worker_timeout_s()
    try:
        try:
            timed_out, stdout_bytes, stderr_bytes = await _run_worker_subprocess(
                cmd=cmd,
                timeout_s=timeout_s,
            )
        except ControlledWorkerExitedError:
            cleanup_fallback_excel_kill = False
            killed_specific, killed_fallback = await _run_cancellation_safe(
                _cleanup_worker_excel_pid_file(
                    platform=platform,
                    pid_file=excel_pid_file,
                    use_fallback_excel_kill=False,
                    baseline_excel_pids=baseline_excel_pids,
                )
            )
            if not killed_specific:
                logger.warning(f"[main] {context} exited without attributed Excel cleanup "
                    f"(fallback_killed={killed_fallback})")
            raise
        except Exception:
            killed_specific, killed_fallback = await _cleanup_worker_excel_pid_file(
                platform=platform,
                pid_file=excel_pid_file,
                use_fallback_excel_kill=use_fallback_excel_kill,
                baseline_excel_pids=baseline_excel_pids,
            )
            if not killed_specific:
                logger.warning(f"[main] {context} failed without attributed Excel cleanup "
                    f"(fallback_killed={killed_fallback})")
            raise
        if timed_out:
            killed_specific, killed_fallback = await _cleanup_worker_excel_pid_file(
                platform=platform,
                pid_file=excel_pid_file,
                use_fallback_excel_kill=use_fallback_excel_kill,
                baseline_excel_pids=baseline_excel_pids,
            )
            if not killed_specific:
                logger.warning(f"[main] {context} timed out without attributed Excel cleanup "
                    f"(fallback_killed={killed_fallback})")
            return {"ok": False, "msg": f"timeout after {timeout_s:.0f}s"}

        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()

        if not stdout_text:
            logger.warning(f"[{log_label}] empty worker response. stderr: {stderr_text}")
            return {"ok": False, "msg": "empty worker response"}

        payload = _parse_worker_stdout(stdout_text, kind=kind)
        if payload is None:
            logger.warning(f"[{log_label}] invalid worker response: {stdout_text!r} "
                f"stderr: {stderr_text}")
            return {"ok": False, "msg": "invalid worker response"}

        ok = bool(payload.get("ok", False))
        msg = _public_worker_message(payload.get("msg"), fallback="")
        payload["ok"] = ok
        payload["msg"] = msg
        if not ok:
            fallback = "worker reported failure" if is_reward else "recalc failed"
            public_msg = msg or fallback
            logger.warning(f"[{log_label}] worker reported failure: {public_msg} stderr: {stderr_text}")
            payload["msg"] = public_msg
        return payload
    except asyncio.CancelledError:
        await _cleanup_worker_excel_pid_file_cancellation_safe(
            platform=platform,
            pid_file=excel_pid_file,
            use_fallback_excel_kill=cleanup_fallback_excel_kill,
            baseline_excel_pids=baseline_excel_pids,
            context=context,
        )
        raise
    finally:
        await _run_cancellation_safe(asyncio.to_thread(_unlink_missing_ok, excel_pid_file))


async def _recalc_file_via_worker(*, proc_file: Path, platform: Platform) -> tuple[bool, str]:
    payload = await _run_worker_subprocess_job(
        JobKind.RECALCULATE,
        proc_file=proc_file,
        platform=platform,
    )
    return bool(payload.get("ok", False)), _public_worker_message(payload.get("msg"), fallback="")


async def _compute_reward_via_worker(
    *,
    gt_file: Path,
    proc_file: Path,
    answer_position: str,
    platform: Platform,
) -> tuple[float, str]:
    payload = await _run_worker_subprocess_job(
        JobKind.REWARD,
        gt_file=gt_file,
        proc_file=proc_file,
        answer_position=answer_position,
        platform=platform,
    )
    if not bool(payload.get("ok", False)):
        raise RuntimeError(_public_worker_message(payload.get("msg"), fallback="worker reported failure"))
    try:
        reward = _parse_worker_reward(payload)
    except RuntimeError as exc:
        logger.warning(f"[_compute_reward_via_worker] invalid worker reward: {payload.get('reward')!r}")
        raise exc
    return reward, _public_worker_message(payload.get("msg"), fallback="")
