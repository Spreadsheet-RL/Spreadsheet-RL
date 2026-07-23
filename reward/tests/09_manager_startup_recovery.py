from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from contextlib import closing, suppress
from pathlib import Path

from _tempdir import temporary_directory

os.environ["REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"] = "1"
os.environ["REWARD_API_PLATFORM"] = "windows"
os.environ["REWARD_API_INSTANCE_PER_WORKER"] = "0"
os.environ["REWARD_API_WORKER_TIMEOUT_S"] = "1"

from async_reward_api import aio as aio_mod  # noqa: E402
from async_reward_api import config as config_mod  # noqa: E402
from async_reward_api import manager as manager_mod  # noqa: E402
from async_reward_api.excel_pool import ExcelWorkerPool  # noqa: E402
from async_reward_api.job_store import SqliteJobStore  # noqa: E402
from async_reward_api.main import RewardJobManager  # noqa: E402
from async_reward_api.models import JobKind, JobRecord, JobStatus  # noqa: E402
from async_reward_api.platform import Platform  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _FakePool:
    def __init__(self, *, size: int = 4) -> None:
        self.size = size
        self.start_calls = 0
        self.shutdown_calls: list[bool] = []

    async def start(self) -> None:
        self.start_calls += 1

    async def acquire(self, *args, **kwargs):
        await asyncio.Event().wait()

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_calls.append(force)

    async def status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "slots": self.size,
            "active_instances": self.size,
            "alive_instances": self.size,
            "available_instances": self.size,
            "restart_pending": 0,
            "recycle_pending": 0,
        }


class _CancellationAwarePool(_FakePool):
    def __init__(self) -> None:
        super().__init__(size=1)
        self.shutdown_started = asyncio.Event()
        self.shutdown_release = asyncio.Event()
        self.cancelled = False
        self.cleaned = False

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_calls.append(force)
        self.shutdown_started.set()
        try:
            await self.shutdown_release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.cleaned = True


class _FakeWindowsOs:
    name = "nt"
    environ = os.environ
    path = os.path
    getpid = staticmethod(os.getpid)
    chmod = staticmethod(os.chmod)


class _ReleaseFailPool(_FakePool):
    def __init__(self) -> None:
        super().__init__(size=1)
        self.worker = object()
        self.release_started = asyncio.Event()

    async def acquire(self, *args, **kwargs):
        return self.worker

    async def release(self, worker) -> None:
        self.release_started.set()
        raise RuntimeError("release failed")


async def _failed_background_task() -> None:
    raise RuntimeError("background loop failed before shutdown")


class _FinishFailStore:
    db_path = Path("finish_fail.sqlite3")

    def finish(self, **kwargs) -> None:
        raise RuntimeError("finish failed")

    def stats(self) -> dict[str, int]:
        return {"queued": 0, "running": 1, "jobs": 1}

    def close(self) -> None:
        pass


class _BlockingFinishStore:
    def __init__(self, delegate: SqliteJobStore) -> None:
        self._delegate = delegate
        self.done_started = threading.Event()
        self.release_done = threading.Event()
        self.error_finishes = 0

    @property
    def db_path(self) -> Path:
        return self._delegate.db_path

    def finish(self, **kwargs) -> bool:
        if kwargs.get("status") is JobStatus.DONE:
            self.done_started.set()
            self.release_done.wait(timeout=5.0)
        if kwargs.get("status") is JobStatus.ERROR:
            self.error_finishes += 1
        return self._delegate.finish(**kwargs)

    def close(self) -> None:
        self._delegate.close()


class _ClaimFailStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.claim_started = threading.Event()

    def has_claimable_jobs(self, max_running_jobs: int | None) -> bool:
        return True

    def claim_next(self, **kwargs):
        self.claim_started.set()
        raise RuntimeError("claim failed")


class _NoJobStore:
    db_path = Path("no_job.sqlite3")

    def has_claimable_jobs(self, max_running_jobs: int | None) -> bool:
        return True

    def claim_next(self, **kwargs):
        return None

    def stats(self) -> dict[str, int]:
        return {"queued": 0, "running": 0, "jobs": 0}

    def close(self) -> None:
        pass


class _CountingStaleSweepStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.quarantine_sweeps = 0
        self.stale_sweeps = 0

    def quarantine_invalid_jobs(self) -> int:
        self.quarantine_sweeps += 1
        return super().quarantine_invalid_jobs()

    def mark_stale_running_as_error(self, *, older_than_s: float, msg: str) -> int:
        self.stale_sweeps += 1
        return super().mark_stale_running_as_error(older_than_s=older_than_s, msg=msg)


class _BlockingHasClaimableStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.has_claimable_started = threading.Event()
        self.release_has_claimable = threading.Event()
        self.has_claimable_completed = threading.Event()
        self.close_called = threading.Event()
        self.close_after_has_claimable = False
        self.timeline: list[str] = []

    def has_claimable_jobs(self, max_running_jobs: int | None) -> bool:
        self.has_claimable_started.set()
        self.release_has_claimable.wait(timeout=5.0)
        self.timeline.append("has_claimable_done")
        self.has_claimable_completed.set()
        return False

    def close(self) -> None:
        self.close_after_has_claimable = self.has_claimable_completed.is_set()
        self.timeline.append("close")
        self.close_called.set()
        super().close()


class _BlockingCleanupBatchStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.list_started = threading.Event()
        self.release_list = threading.Event()
        self.list_completed = threading.Event()

    def list_cleanup_batch(self, **kwargs):
        self.list_started.set()
        self.release_list.wait(timeout=5.0)
        self.list_completed.set()
        return []


class _ExecutorProbeStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.lock = threading.Lock()
        self.release_calls = threading.Event()
        self.two_calls_started = threading.Event()
        self.active_calls = 0
        self.max_active_calls = 0
        self.thread_names: set[str] = set()
        self.close_saw_active_call = False

    def get_snapshot(self, _job_id: str) -> None:
        with self.lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            self.thread_names.add(threading.current_thread().name)
            if self.active_calls >= 2:
                self.two_calls_started.set()
        try:
            self.release_calls.wait(timeout=5.0)
            return None
        finally:
            with self.lock:
                self.active_calls -= 1

    def close(self) -> None:
        with self.lock:
            self.close_saw_active_call = self.active_calls > 0
        super().close()


class _FailOnceInitStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.fail_next_init = True

    def init(self) -> None:
        super().init()
        if self.fail_next_init:
            self.fail_next_init = False
            raise RuntimeError("injected init failure")


class _BlockingInitStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.block_next_init = True
        self.init_started = threading.Event()
        self.release_init = threading.Event()
        self.init_completed = threading.Event()

    def init(self) -> None:
        super().init()
        if not self.block_next_init:
            return
        self.block_next_init = False
        self.init_started.set()
        released = self.release_init.wait(timeout=5.0)
        self.init_completed.set()
        if not released:
            raise RuntimeError("timed out waiting to release init")


class _ImmediateTimeoutStop:
    def is_set(self) -> bool:
        return False

    async def wait(self) -> None:
        raise asyncio.TimeoutError


async def main_async() -> int:
    original_diagnostics_dir = os.environ.get("REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR")
    os.environ.pop("REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR", None)
    with temporary_directory(prefix="async_reward_api_startup_recovery_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()

        old_started_at = time.time() - 120.0
        stale_job = JobRecord(
            job_id="stale-running-job",
            thread_dir="thread_1",
            gt_file=tmp_path / "target.xlsx",
            proc_file=tmp_path / "output.xlsx",
            answer_position="Sheet1!A1",
            status=JobStatus.RUNNING,
            created_at_s=old_started_at,
            started_at_s=old_started_at,
        )
        _assert(store.enqueue(stale_job, max_queue_size=10), "failed to seed stale running job")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                    ("seed-worker", stale_job.job_id),
                )

        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        await manager.start()
        initial_store_executor = manager._store_executor
        _assert(initial_store_executor is not None, "start did not create the SQLite executor")
        _assert(
            manager._windows_excel_diagnostics_cleanup_task is None,
            "diagnostics cleanup should be opt-in",
        )
        snapshot = await manager.get_snapshot(stale_job.job_id)

        _assert(snapshot is not None, "stale job disappeared")
        _assert(snapshot.status is JobStatus.ERROR, f"stale job was not marked error: {snapshot}")
        _assert(
            "stale running job" in snapshot.msg,
            f"stale job message was not preserved: {snapshot.msg!r}",
        )
        stats = await manager.stats()
        _assert(stats.get("ready") is True, f"manager was not ready before restart test: {stats}")
        original_health_failure_ttl = os.environ.get("REWARD_API_HEALTH_FAILURE_TTL_S")
        os.environ["REWARD_API_HEALTH_FAILURE_TTL_S"] = "600"
        try:
            manager._record_job_task_error("restart test failure", RuntimeError("previous lifecycle failure"))
            failed_stats = await manager.stats()
            _assert(failed_stats.get("ready") is False, "injected job task failure did not degrade readiness")
            job_tasks = failed_stats.get("job_tasks")
            _assert(isinstance(job_tasks, dict), f"job task stats missing after failure: {failed_stats}")
            _assert(int(job_tasks.get("failures") or 0) > 0, f"job task failure count missing: {failed_stats}")
            manager._last_job_task_failure_s = time.time() - 601.0
            recovered_after_ttl_stats = await manager.stats()
            _assert(
                recovered_after_ttl_stats.get("ready") is True,
                f"old job task failure still degraded readiness: {recovered_after_ttl_stats}",
            )
            recovered_job_tasks = recovered_after_ttl_stats.get("job_tasks")
            _assert(
                isinstance(recovered_job_tasks, dict)
                and int(recovered_job_tasks.get("failures") or 0) > 0,
                f"TTL recovery hid cumulative failures: {recovered_after_ttl_stats}",
            )
            os.environ["REWARD_API_HEALTH_FAILURE_TTL_S"] = "0"
            sticky_stats = await manager.stats()
            _assert(sticky_stats.get("ready") is False, f"TTL=0 did not keep failure sticky: {sticky_stats}")
        finally:
            if original_health_failure_ttl is None:
                os.environ.pop("REWARD_API_HEALTH_FAILURE_TTL_S", None)
            else:
                os.environ["REWARD_API_HEALTH_FAILURE_TTL_S"] = original_health_failure_ttl
        await manager.shutdown()
        _assert(manager._store_executor is None, "shutdown did not release the SQLite executor")
        with store._connections_lock:
            _assert(store._closed, "shutdown did not mark job store closed")
            _assert(not store._connections, "shutdown did not close pooled SQLite handles")
        try:
            await manager.get_snapshot(stale_job.job_id)
        except RuntimeError as exc:
            _assert("shut down" in str(exc), f"post-shutdown store error lost context: {exc}")
        else:
            raise AssertionError("post-shutdown store call did not fail")
        _assert(manager._store_executor is None, "post-shutdown store call recreated the SQLite executor")
        await manager.start()
        _assert(
            manager._store_executor is not None and manager._store_executor is not initial_store_executor,
            "restart did not create a fresh SQLite executor",
        )
        with store._connections_lock:
            _assert(not store._closed, "restart did not reopen job store")
        try:
            restarted_stats = await manager.stats()
            _assert(
                restarted_stats.get("ready") is True,
                f"manager restart did not reset lifecycle state: {restarted_stats}",
            )
            job_tasks = restarted_stats.get("job_tasks")
            _assert(isinstance(job_tasks, dict), f"restart job task stats missing: {restarted_stats}")
            _assert(job_tasks.get("failures") == 0, f"restart did not reset job task failures: {restarted_stats}")
            try:
                await manager.start()
            except RuntimeError as exc:
                _assert("already started" in str(exc), f"double-start error lost context: {exc}")
            else:
                raise AssertionError("double-start did not fail")
        finally:
            await manager.shutdown()
        _assert(manager._store_executor is None, "restart shutdown kept the SQLite executor")
        with store._connections_lock:
            _assert(store._closed, "restart shutdown did not mark job store closed")
            _assert(not store._connections, "restart shutdown did not close pooled SQLite handles")
        store.init()
        with store._connections_lock:
            _assert(not store._closed, "store init did not reopen after shutdown")
        store.finish(
            job_id=stale_job.job_id,
            status=JobStatus.DONE,
            reward=1.0,
            msg="late worker success",
        )
        snapshot_after_late_finish = store.get_snapshot(stale_job.job_id)
        _assert(snapshot_after_late_finish is not None, "stale job disappeared after late finish")
        _assert(
            snapshot_after_late_finish.status is JobStatus.ERROR,
            f"late finish overwrote terminal stale state: {snapshot_after_late_finish}",
        )
        store.close()
    if original_diagnostics_dir is not None:
        os.environ["REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR"] = original_diagnostics_dir

    with temporary_directory(prefix="async_reward_api_failed_start_cleanup_") as tmp:
        tmp_path = Path(tmp)
        store = _FailOnceInitStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        executor_thread_prefix = f"reward-sqlite-{manager._instance_id[:8]}"
        try:
            await manager.start()
        except RuntimeError as exc:
            _assert("injected init failure" in str(exc), f"unexpected failed-start error: {exc}")
        else:
            raise AssertionError("injected store init failure did not fail start")
        _assert(manager._store_executor is None, "failed start retained the SQLite executor")
        _assert(not manager._store_executor_closing, "failed start left the SQLite executor closing")
        _assert(not manager._store_calls_allowed, "failed start left store calls enabled")
        _assert(
            not any(thread.name.startswith(executor_thread_prefix) for thread in threading.enumerate()),
            "failed start leaked a SQLite executor thread",
        )
        with store._connections_lock:
            _assert(store._closed, "failed start did not close the job store")
            _assert(not store._connections, "failed start retained pooled SQLite handles")
        await manager.start()
        try:
            stats = await manager.stats()
            _assert(stats.get("ready") is True, f"manager did not recover after failed start: {stats}")
        finally:
            await manager.shutdown()

    with temporary_directory(prefix="async_reward_api_cancelled_start_cleanup_") as tmp:
        tmp_path = Path(tmp)
        store = _BlockingInitStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        executor_thread_prefix = f"reward-sqlite-{manager._instance_id[:8]}"
        start_task = asyncio.create_task(manager.start())
        try:
            init_started = await asyncio.to_thread(store.init_started.wait, 2.0)
            _assert(init_started, "cancelled-start test did not enter store init")
            start_task.cancel()
            await asyncio.sleep(0.05)
            _assert(not start_task.done(), "cancelled start did not wait for in-flight store init")
            store.release_init.set()
            try:
                await asyncio.wait_for(start_task, timeout=2.0)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled start did not propagate cancellation")
        finally:
            store.release_init.set()
            if not start_task.done():
                start_task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(start_task, timeout=2.0)
        _assert(store.init_completed.is_set(), "cancelled start did not drain in-flight store init")
        _assert(manager._store_executor is None, "cancelled start retained the SQLite executor")
        _assert(not manager._store_executor_closing, "cancelled start left the SQLite executor closing")
        _assert(not manager._store_calls_allowed, "cancelled start left store calls enabled")
        _assert(
            not any(thread.name.startswith(executor_thread_prefix) for thread in threading.enumerate()),
            "cancelled start leaked a SQLite executor thread",
        )
        with store._connections_lock:
            _assert(store._closed, "cancelled start did not close the job store")
            _assert(not store._connections, "cancelled start retained pooled SQLite handles")
        await manager.start()
        await manager.shutdown()

    with temporary_directory(prefix="async_reward_api_store_executor_") as tmp:
        tmp_path = Path(tmp)
        original_executor_workers = os.environ.get("REWARD_API_SQLITE_EXECUTOR_WORKERS")
        os.environ["REWARD_API_SQLITE_EXECUTOR_WORKERS"] = "2"
        store = _ExecutorProbeStore(tmp_path / "jobs.sqlite3")
        store.init()
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        snapshot_tasks = [
            asyncio.create_task(manager.get_snapshot(f"probe-{index}"))
            for index in range(8)
        ]
        try:
            two_started = await asyncio.to_thread(store.two_calls_started.wait, 2.0)
            _assert(two_started, "dedicated SQLite executor did not run two store calls concurrently")
            with store.lock:
                _assert(
                    store.max_active_calls == 2,
                    f"SQLite executor ignored configured bound: {store.max_active_calls}",
                )
                _assert(
                    len(store.thread_names) == 2
                    and all(name.startswith("reward-sqlite-") for name in store.thread_names),
                    f"store calls did not use dedicated SQLite threads: {store.thread_names}",
                )
            _assert(manager._store_executor is not None, "SQLite executor was not created lazily")
        finally:
            store.release_calls.set()
            await asyncio.gather(*snapshot_tasks, return_exceptions=True)
            await manager.shutdown()
            if original_executor_workers is None:
                os.environ.pop("REWARD_API_SQLITE_EXECUTOR_WORKERS", None)
            else:
                os.environ["REWARD_API_SQLITE_EXECUTOR_WORKERS"] = original_executor_workers
        _assert(not store.close_saw_active_call, "store closed before dedicated executor calls drained")
        _assert(manager._store_executor is None, "shutdown kept the dedicated SQLite executor")
        _assert(not manager._store_executor_closing, "shutdown left the SQLite executor closing")

    with temporary_directory(prefix="async_reward_api_shutdown_failure_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        failed_task = asyncio.create_task(_failed_background_task())
        await asyncio.sleep(0)
        _assert(failed_task.done(), "failed background task did not finish")

        fake_pool = _FakePool()
        manager._cleanup_task = failed_task
        manager._excel_pool = fake_pool
        await manager.shutdown()
        _assert(fake_pool.shutdown_calls == [True], "shutdown skipped Excel pool cleanup")
        _assert(manager._excel_pool is None, "shutdown did not clear Excel pool")

    with temporary_directory(prefix="async_reward_api_cancelled_manager_shutdown_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        await manager._run_store(store.init)
        executor_thread_prefix = f"reward-sqlite-{manager._instance_id[:8]}"
        fake_pool = _CancellationAwarePool()
        manager._excel_pool = fake_pool
        shutdown_task = asyncio.create_task(manager.shutdown())
        await asyncio.wait_for(fake_pool.shutdown_started.wait(), timeout=1.0)
        shutdown_task.cancel()
        await asyncio.sleep(0)
        _assert(not shutdown_task.done(), "cancelled manager shutdown abandoned cleanup")
        _assert(not fake_pool.cancelled, "manager cancellation interrupted Excel pool cleanup")
        fake_pool.shutdown_release.set()
        try:
            await asyncio.wait_for(shutdown_task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled manager shutdown did not propagate cancellation")
        _assert(fake_pool.cleaned, "cancelled manager shutdown did not clean Excel pool")
        _assert(manager._excel_pool is None, "cancelled manager shutdown retained Excel pool")
        _assert(manager._store_executor is None, "cancelled manager shutdown retained SQLite executor")
        _assert(not manager._store_calls_allowed, "cancelled manager shutdown left store calls enabled")
        with store._connections_lock:
            _assert(store._closed, "cancelled manager shutdown did not close job store")
            _assert(not store._connections, "cancelled manager shutdown retained SQLite handles")
        _assert(
            not any(thread.name.startswith(executor_thread_prefix) for thread in threading.enumerate()),
            "cancelled manager shutdown leaked a SQLite executor thread",
        )

    with temporary_directory(prefix="async_reward_api_cancelled_job_wait_shutdown_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        await manager._run_store(store.init)
        executor_thread_prefix = f"reward-sqlite-{manager._instance_id[:8]}"
        job_release = asyncio.Event()

        async def blocking_job() -> None:
            await job_release.wait()

        job_task = asyncio.create_task(blocking_job())
        manager._job_tasks.add(job_task)
        job_task.add_done_callback(manager._on_job_task_done)
        fake_pool = _FakePool(size=1)
        manager._excel_pool = fake_pool
        await asyncio.sleep(0)
        shutdown_task = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0.01)
        _assert(manager._stop.is_set(), "job-wait shutdown implementation did not start")
        _assert(not shutdown_task.done(), "job-wait shutdown did not wait for the active job")
        shutdown_task.cancel()
        await asyncio.sleep(0)
        _assert(not shutdown_task.done(), "cancelled job wait abandoned manager cleanup")
        _assert(manager._excel_pool is fake_pool, "cancelled job wait skipped to partial pool cleanup")
        job_release.set()
        try:
            await asyncio.wait_for(shutdown_task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled job-wait shutdown did not propagate cancellation")
        _assert(job_task.done(), "cancelled job-wait shutdown did not drain the job task")
        _assert(fake_pool.shutdown_calls == [True], "cancelled job wait skipped Excel pool cleanup")
        _assert(manager._excel_pool is None, "cancelled job wait retained Excel pool")
        _assert(manager._store_executor is None, "cancelled job wait retained SQLite executor")
        with store._connections_lock:
            _assert(store._closed, "cancelled job wait did not close job store")
            _assert(not store._connections, "cancelled job wait retained SQLite handles")
        _assert(
            not any(thread.name.startswith(executor_thread_prefix) for thread in threading.enumerate()),
            "cancelled job wait leaked a SQLite executor thread",
        )

    with temporary_directory(prefix="async_reward_api_bounded_shutdown_") as tmp:
        tmp_path = Path(tmp)
        manager = RewardJobManager(
            store=SqliteJobStore(tmp_path / "jobs.sqlite3"),
            platform=Platform.WINDOWS,
        )
        release_stubborn_task = asyncio.Event()

        async def stubborn_job_task() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                while not release_stubborn_task.is_set():
                    try:
                        await release_stubborn_task.wait()
                    except asyncio.CancelledError:
                        pass

        original_shutdown_timeout = manager_mod._JOB_TASK_SHUTDOWN_TIMEOUT_S
        manager_mod._JOB_TASK_SHUTDOWN_TIMEOUT_S = 0.01
        stubborn_task = asyncio.create_task(stubborn_job_task())
        manager._job_tasks.add(stubborn_task)
        stubborn_task.add_done_callback(manager._on_job_task_done)
        try:
            await asyncio.sleep(0)
            await manager.shutdown()
            _assert(stubborn_task in manager._job_tasks, "shutdown lost track of timed-out job task")
            _assert(manager._job_task_failures == 1, "shutdown timeout did not degrade job task health")
            _assert(not manager._store._closed, "shutdown closed store while timed-out job task was alive")
            deferred_close = manager._deferred_store_close_task
            _assert(deferred_close is not None, "shutdown did not defer store close for timed-out job task")
            _assert(not deferred_close.done(), "deferred store close finished before timed-out job task")
            _assert(
                "shutdown timed out" in manager._last_job_task_error,
                f"shutdown timeout lost diagnostic detail: {manager._last_job_task_error!r}",
            )
            try:
                await manager.start()
            except RuntimeError as exc:
                _assert("active job tasks" in str(exc), f"unexpected restart block error: {exc}")
            else:
                raise AssertionError("manager restarted while timed-out job task was still alive")
            _assert(
                manager._deferred_store_close_task is deferred_close,
                "blocked start disarmed deferred store close",
            )
            _assert(not deferred_close.done(), "blocked start completed deferred store close")
            await manager.shutdown()
            deferred_close = manager._deferred_store_close_task
            _assert(
                deferred_close is not None,
                "repeated shutdown did not recreate deferred store close for timed-out job task",
            )
            _assert(not deferred_close.done(), "repeated shutdown deferred store close finished too early")
            _assert(not manager._store._closed, "repeated shutdown closed store while job task was alive")
            release_stubborn_task.set()
            await asyncio.wait_for(stubborn_task, timeout=1.0)
            await asyncio.wait_for(deferred_close, timeout=1.0)
            with manager._store._connections_lock:
                _assert(manager._store._closed, "deferred store close did not close the store")
                _assert(not manager._store._connections, "deferred store close left pooled handles open")

            await manager.start()
            try:
                restart_release_stubborn_task = asyncio.Event()

                async def restart_stubborn_job_task() -> None:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        while not restart_release_stubborn_task.is_set():
                            try:
                                await restart_release_stubborn_task.wait()
                            except asyncio.CancelledError:
                                pass

                restart_stubborn_task = asyncio.create_task(restart_stubborn_job_task())
                manager._job_tasks.add(restart_stubborn_task)
                restart_stubborn_task.add_done_callback(manager._on_job_task_done)
                await asyncio.sleep(0)
                await manager.shutdown()
                restart_deferred_close = manager._deferred_store_close_task
                _assert(
                    restart_deferred_close is not None,
                    "restart-race shutdown did not defer store close",
                )
                _assert(
                    not restart_deferred_close.done(),
                    "restart-race deferred store close finished too early",
                )
                _assert(not manager._store._closed, "restart-race shutdown closed store too early")
                restart_release_stubborn_task.set()
                await asyncio.wait_for(restart_stubborn_task, timeout=1.0)
                await manager.start()
                try:
                    _assert(not manager._store._closed, "start allowed deferred close to close new lifecycle")
                    _assert(
                        manager._deferred_store_close_task is None,
                        "start did not clear deferred store close handle",
                    )
                finally:
                    await manager.shutdown()
            finally:
                if manager._deferred_store_close_task is not None:
                    manager._deferred_store_close_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await manager._deferred_store_close_task
                    manager._deferred_store_close_task = None
        finally:
            release_stubborn_task.set()
            manager_mod._JOB_TASK_SHUTDOWN_TIMEOUT_S = original_shutdown_timeout
            if not stubborn_task.done():
                await asyncio.wait_for(stubborn_task, timeout=1.0)

    with temporary_directory(prefix="async_reward_api_shutdown_has_claimable_") as tmp:
        tmp_path = Path(tmp)
        store = _BlockingHasClaimableStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        await manager.start()
        shutdown_task: asyncio.Task | None = None
        try:
            started = await asyncio.to_thread(store.has_claimable_started.wait, 5.0)
            _assert(started, "worker loop did not start has_claimable_jobs")
            shutdown_task = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(0.05)
            _assert(not store.close_called.is_set(), "shutdown closed store while has_claimable_jobs was in flight")
            store.release_has_claimable.set()
            await asyncio.wait_for(shutdown_task, timeout=2.0)
            _assert(store.close_called.is_set(), "shutdown did not close store after has_claimable_jobs")
            _assert(store.close_after_has_claimable, f"store close raced blocked has_claimable_jobs: {store.timeline}")
            _assert(
                store.timeline == ["has_claimable_done", "close"],
                f"unexpected has_claimable shutdown order: {store.timeline}",
            )
        finally:
            store.release_has_claimable.set()
            if shutdown_task is not None and not shutdown_task.done():
                await asyncio.wait_for(shutdown_task, timeout=2.0)
            elif not manager._store._closed:
                await manager.shutdown()

    with temporary_directory(prefix="async_reward_api_cleanup_cancel_store_call_") as tmp:
        tmp_path = Path(tmp)
        original_keep_files = os.environ.get("REWARD_API_KEEP_FILES")
        os.environ["REWARD_API_KEEP_FILES"] = "1"
        cleanup_task: asyncio.Task | None = None
        store = _BlockingCleanupBatchStore(tmp_path / "jobs.sqlite3")
        manager = None
        try:
            store.init()
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            cleanup_task = asyncio.create_task(manager._cleanup_finished_jobs_once(pass_now_s=time.time()))
            started = await asyncio.to_thread(store.list_started.wait, 5.0)
            _assert(started, "cleanup did not start list_cleanup_batch")
            cleanup_task.cancel()
            await asyncio.sleep(0.05)
            _assert(not cleanup_task.done(), "cancelled cleanup did not wait for list_cleanup_batch")
            store.release_list.set()
            try:
                await asyncio.wait_for(cleanup_task, timeout=2.0)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled cleanup store call did not propagate cancellation")
            _assert(store.list_completed.is_set(), "cleanup cancellation did not wait for list_cleanup_batch")
        finally:
            store.release_list.set()
            if cleanup_task is not None and not cleanup_task.done():
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(cleanup_task, timeout=2.0)
            if manager is not None:
                await manager.shutdown()
            else:
                store.close()
            if original_keep_files is None:
                os.environ.pop("REWARD_API_KEEP_FILES", None)
            else:
                os.environ["REWARD_API_KEEP_FILES"] = original_keep_files

    with temporary_directory(prefix="async_reward_api_pool_concurrency_") as tmp:
        tmp_path = Path(tmp)
        manager = RewardJobManager(
            store=SqliteJobStore(tmp_path / "jobs.sqlite3"),
            platform=Platform.WINDOWS,
        )
        manager._excel_pool = _FakePool(size=4)
        manager._max_running_jobs = 2
        _assert(
            manager._effective_worker_concurrency() == 2,
            "pool concurrency ignored max_running_jobs cap",
        )
        manager._max_running_jobs = None
        _assert(
            manager._effective_worker_concurrency() == 4,
            "pool concurrency changed without max_running_jobs cap",
        )
        manager._store.close()

    with temporary_directory(prefix="async_reward_api_real_pool_stats_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        manager = RewardJobManager(
            store=store,
            platform=Platform.WINDOWS,
        )
        manager._excel_pool = ExcelWorkerPool(size=1, platform="windows")
        try:
            stats = await manager.stats()
            excel_pool = stats.get("excel_pool")
            _assert(isinstance(excel_pool, dict), f"real Excel pool stats missing: {stats}")
            _assert(excel_pool.get("mode") == "persistent", f"real Excel pool stats changed: {excel_pool}")
            _assert(excel_pool.get("startup_failed") is False, f"real Excel pool startup flag changed: {excel_pool}")
        finally:
            await manager.shutdown()

    with temporary_directory(prefix="async_reward_api_manager_owner_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        first_manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        second_manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        _assert(
            first_manager._worker_id != second_manager._worker_id,
            "same-process managers reused the same worker owner id",
        )
        lease_now = time.time()
        _assert(
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id=first_manager._worker_id,
                lease_s=60.0,
                now_s=lease_now,
            ),
            "first manager did not acquire cleanup lease",
        )
        _assert(
            not store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id=second_manager._worker_id,
                lease_s=60.0,
                now_s=lease_now + 1.0,
            ),
            "same-process second manager renewed another manager's cleanup lease",
        )
        store.close()

    with temporary_directory(prefix="async_reward_api_startup_stale_lease_") as tmp:
        tmp_path = Path(tmp)
        startup_store = _CountingStaleSweepStore(tmp_path / "jobs.sqlite3")
        first_manager = RewardJobManager(store=startup_store, platform=Platform.WINDOWS)
        second_manager = RewardJobManager(store=startup_store, platform=Platform.WINDOWS)
        await first_manager.start()
        try:
            await second_manager.start()
            try:
                _assert(
                    startup_store.stale_sweeps == 1,
                    f"startup stale sweep was not leader-elected: {startup_store.stale_sweeps}",
                )
                _assert(
                    startup_store.quarantine_sweeps == 1,
                    f"startup quarantine sweep was not leader-elected: {startup_store.quarantine_sweeps}",
                )
            finally:
                await second_manager.shutdown()
        finally:
            await first_manager.shutdown()

    with temporary_directory(prefix="async_reward_api_quarantine_lease_") as tmp:
        tmp_path = Path(tmp)
        original_quarantine_interval = os.environ.get("REWARD_API_QUARANTINE_SWEEP_INTERVAL_S")
        os.environ["REWARD_API_QUARANTINE_SWEEP_INTERVAL_S"] = "600"
        manager = None
        first_manager = None
        second_manager = None
        try:
            store = _CountingStaleSweepStore(tmp_path / "jobs.sqlite3")
            store.init()
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            first_now = 10_000.0
            await manager._sweep_stale_running_jobs_once(pass_now_s=first_now)
            _assert(store.quarantine_sweeps == 1, f"first quarantine sweep did not run: {store.quarantine_sweeps}")
            await manager._sweep_stale_running_jobs_once(pass_now_s=first_now + 1.0)
            _assert(
                store.quarantine_sweeps == 1,
                f"immediate quarantine sweep was not throttled: {store.quarantine_sweeps}",
            )
            await manager._sweep_stale_running_jobs_once(pass_now_s=first_now + 601.0)
            _assert(
                store.quarantine_sweeps == 2,
                f"expired quarantine lease did not permit sweep: {store.quarantine_sweeps}",
            )

            os.environ["REWARD_API_QUARANTINE_SWEEP_INTERVAL_S"] = "0"
            zero_store = _CountingStaleSweepStore(tmp_path / "interval_zero_jobs.sqlite3")
            zero_store.init()
            first_manager = RewardJobManager(store=zero_store, platform=Platform.WINDOWS)
            second_manager = RewardJobManager(store=zero_store, platform=Platform.WINDOWS)
            zero_now = 20_000.0
            await first_manager._sweep_stale_running_jobs_once(pass_now_s=zero_now)
            _assert(
                zero_store.quarantine_sweeps == 1,
                f"interval=0 first stale-owned quarantine did not run: {zero_store.quarantine_sweeps}",
            )
            await second_manager._sweep_stale_running_jobs_once(pass_now_s=zero_now + 1.0)
            _assert(
                zero_store.quarantine_sweeps == 1,
                f"interval=0 quarantine ran without stale lease: {zero_store.quarantine_sweeps}",
            )
            await first_manager._sweep_stale_running_jobs_once(pass_now_s=zero_now + 2.0)
            _assert(
                zero_store.quarantine_sweeps == 2,
                f"interval=0 stale owner did not run each pass: {zero_store.quarantine_sweeps}",
            )
        finally:
            if manager is not None:
                await manager.shutdown()
            if first_manager is not None:
                await first_manager.shutdown()
            if second_manager is not None:
                await second_manager.shutdown()
            if original_quarantine_interval is None:
                os.environ.pop("REWARD_API_QUARANTINE_SWEEP_INTERVAL_S", None)
            else:
                os.environ["REWARD_API_QUARANTINE_SWEEP_INTERVAL_S"] = original_quarantine_interval

    malformed_cleanup_path = Path("bad\x00cleanup")
    _assert(
        not await aio_mod._unlink_with_retries(malformed_cleanup_path, (0.0,)),
        "malformed unlink cleanup path was reported successful",
    )
    _assert(
        not await aio_mod._rmtree_with_retries(malformed_cleanup_path, (0.0,)),
        "malformed rmtree cleanup path was reported successful",
    )

    original_get_diagnostics_dir = manager_mod._get_windows_excel_diagnostics_dir
    original_is_safe_diagnostics_dir = manager_mod._is_safe_windows_excel_diagnostics_dir
    original_delete_dir_contents = manager_mod._delete_dir_contents
    safe_diagnostics_dir = Path("C:/Users/admin/AppData/Local/Temp/Diagnostics/safe")
    unsafe_diagnostics_dir = Path("C:/Users/admin/AppData/Local/Temp/unsafe")
    diagnostics_dirs = [safe_diagnostics_dir, unsafe_diagnostics_dir]
    diagnostics_deleted: list[Path] = []
    try:
        manager_mod._get_windows_excel_diagnostics_dir = lambda: diagnostics_dirs.pop(0)
        manager_mod._is_safe_windows_excel_diagnostics_dir = lambda path: path == safe_diagnostics_dir
        manager_mod._delete_dir_contents = lambda path: diagnostics_deleted.append(path)
        manager = RewardJobManager(store=SqliteJobStore(Path("diagnostics.sqlite3")), platform=Platform.WINDOWS)
        manager._stop = _ImmediateTimeoutStop()
        try:
            await manager._windows_excel_diagnostics_cleanup_loop()
        except RuntimeError as exc:
            _assert("unsafe" in str(exc), f"unexpected diagnostics cleanup error: {exc}")
        else:
            raise AssertionError("diagnostics cleanup did not re-check unsafe target")
        _assert(
            diagnostics_deleted == [safe_diagnostics_dir],
            f"diagnostics cleanup touched unsafe target: {diagnostics_deleted}",
        )
    finally:
        manager_mod._get_windows_excel_diagnostics_dir = original_get_diagnostics_dir
        manager_mod._is_safe_windows_excel_diagnostics_dir = original_is_safe_diagnostics_dir
        manager_mod._delete_dir_contents = original_delete_dir_contents

    with temporary_directory(prefix="async_reward_api_pool_start_size_") as tmp:
        tmp_path = Path(tmp)
        original_pool_class = manager_mod.ExcelWorkerPool
        original_manager_os = manager_mod.os
        original_config_os = config_mod.os
        original_instance_per_worker = os.environ.get("REWARD_API_INSTANCE_PER_WORKER")
        original_max_running_jobs = os.environ.get("REWARD_API_MAX_RUNNING_JOBS")
        created_sizes: list[int] = []

        class _StartupSizePool(_FakePool):
            def __init__(self, *, size: int, platform: str) -> None:
                super().__init__(size=size)
                created_sizes.append(size)

        os.environ["REWARD_API_INSTANCE_PER_WORKER"] = "4"
        os.environ["REWARD_API_MAX_RUNNING_JOBS"] = "2"
        manager_mod.os = _FakeWindowsOs()
        config_mod.os = _FakeWindowsOs()
        manager_mod.ExcelWorkerPool = _StartupSizePool
        try:
            manager = RewardJobManager(
                store=SqliteJobStore(tmp_path / "jobs.sqlite3"),
                platform=Platform.WINDOWS,
            )
            await manager.start()
            try:
                _assert(created_sizes == [2], f"pool startup size ignored max_running_jobs: {created_sizes}")
                stats = await manager.stats()
                _assert(stats["concurrency"] == 2, f"pool startup cap changed concurrency: {stats}")
            finally:
                await manager.shutdown()
        finally:
            manager_mod.os = original_manager_os
            config_mod.os = original_config_os
            manager_mod.ExcelWorkerPool = original_pool_class
            if original_instance_per_worker is None:
                os.environ.pop("REWARD_API_INSTANCE_PER_WORKER", None)
            else:
                os.environ["REWARD_API_INSTANCE_PER_WORKER"] = original_instance_per_worker
            if original_max_running_jobs is None:
                os.environ.pop("REWARD_API_MAX_RUNNING_JOBS", None)
            else:
                os.environ["REWARD_API_MAX_RUNNING_JOBS"] = original_max_running_jobs

    with temporary_directory(prefix="async_reward_api_pool_restart_cap_") as tmp:
        tmp_path = Path(tmp)
        original_pool_class = manager_mod.ExcelWorkerPool
        original_manager_os = manager_mod.os
        original_config_os = config_mod.os
        original_instance_per_worker = os.environ.get("REWARD_API_INSTANCE_PER_WORKER")
        original_max_running_jobs = os.environ.get("REWARD_API_MAX_RUNNING_JOBS")
        created_sizes: list[int] = []
        start_attempts = {"count": 0}

        class _RestartPool(_FakePool):
            def __init__(self, *, size: int, platform: str) -> None:
                super().__init__(size=size)
                created_sizes.append(size)

            async def start(self) -> None:
                start_attempts["count"] += 1
                if start_attempts["count"] == 1:
                    raise RuntimeError("transient pool start failure")
                await super().start()

        os.environ["REWARD_API_INSTANCE_PER_WORKER"] = "4"
        os.environ.pop("REWARD_API_MAX_RUNNING_JOBS", None)
        manager_mod.os = _FakeWindowsOs()
        config_mod.os = _FakeWindowsOs()
        manager_mod.ExcelWorkerPool = _RestartPool
        try:
            manager = RewardJobManager(
                store=SqliteJobStore(tmp_path / "jobs.sqlite3"),
                platform=Platform.WINDOWS,
            )
            await manager.start()
            failed_stats = await manager.stats()
            _assert(failed_stats["concurrency"] == 1, f"pool failure fallback did not cap concurrency: {failed_stats}")
            await manager.shutdown()

            await manager.start()
            try:
                recovered_stats = await manager.stats()
                _assert(created_sizes == [4, 4], f"pool restart did not restore configured size: {created_sizes}")
                _assert(
                    recovered_stats["concurrency"] == 4,
                    f"pool restart kept stale fallback cap: {recovered_stats}",
                )
                _assert(
                    recovered_stats.get("excel_pool_healthy") is True,
                    f"pool restart did not clear startup failure health: {recovered_stats}",
                )
            finally:
                await manager.shutdown()
        finally:
            manager_mod.os = original_manager_os
            config_mod.os = original_config_os
            manager_mod.ExcelWorkerPool = original_pool_class
            if original_instance_per_worker is None:
                os.environ.pop("REWARD_API_INSTANCE_PER_WORKER", None)
            else:
                os.environ["REWARD_API_INSTANCE_PER_WORKER"] = original_instance_per_worker
            if original_max_running_jobs is None:
                os.environ.pop("REWARD_API_MAX_RUNNING_JOBS", None)
            else:
                os.environ["REWARD_API_MAX_RUNNING_JOBS"] = original_max_running_jobs

    with temporary_directory(prefix="async_reward_api_worker_loop_health_") as tmp:
        tmp_path = Path(tmp)
        store = _ClaimFailStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        await manager.start()
        try:
            claim_started = await asyncio.to_thread(store.claim_started.wait, 5.0)
            _assert(claim_started, "worker loop did not attempt a failing claim")
            stats = await manager.stats()
            _assert(stats.get("ready") is False, f"worker loop failure did not degrade readiness: {stats}")
            _assert(
                stats.get("background_tasks_healthy") is False,
                f"worker loop failure did not degrade background task health: {stats}",
            )
            background_errors = stats.get("background_loop_errors")
            _assert(isinstance(background_errors, dict), "background loop error stats missing")
            _assert(
                int(background_errors.get("failures") or 0) >= 1,
                f"worker loop failure count changed: {stats}",
            )
            _assert(
                "job acquisition failed" in str(background_errors.get("last_error")),
                f"worker loop last error lost context: {stats}",
            )
        finally:
            await manager.shutdown()

    release_fail_pool = _ReleaseFailPool()
    release_fail_manager = RewardJobManager(store=_NoJobStore(), platform=Platform.WINDOWS)
    release_fail_manager._excel_pool = release_fail_pool
    release_fail_manager._run_sem = asyncio.Semaphore(1)
    release_fail_manager._poll_interval_s = 30.0
    release_fail_manager._idle_poll_max_s = 30.0
    release_fail_task = asyncio.create_task(release_fail_manager._worker_loop())
    try:
        await asyncio.wait_for(release_fail_pool.release_started.wait(), timeout=2.0)
        _assert(
            release_fail_manager._run_sem._value == 1,
            "worker loop leaked a semaphore permit after pool release failure",
        )
        release_fail_stats = await release_fail_manager.stats()
        _assert(
            release_fail_stats.get("background_tasks_healthy") is False,
            f"pool release failure did not degrade readiness: {release_fail_stats}",
        )
    finally:
        release_fail_manager._stop.set()
        release_fail_manager._submit_wake.set()
        if not release_fail_task.done():
            release_fail_task.cancel()
        await asyncio.gather(release_fail_task, return_exceptions=True)
        await release_fail_manager.shutdown()

    with temporary_directory(prefix="async_reward_api_finish_failure_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        output_root.mkdir()
        proc_file = output_root / "output.xlsx"
        proc_file.write_bytes(b"placeholder")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        manager = None
        try:
            manager = RewardJobManager(store=_FinishFailStore(), platform=Platform.WINDOWS)
            manager._run_sem = asyncio.Semaphore(1)

            async def fast_reward(job, *, excel_worker, use_excel_pool):
                return 1.0, "fast"

            manager._compute_reward = fast_reward
            await manager._run_job(
                JobRecord(
                    job_id="finish-fail",
                    thread_dir="thread_1",
                    gt_file=output_root / "target.xlsx",
                    proc_file=proc_file,
                    answer_position="Sheet1!A1",
                    status=JobStatus.RUNNING,
                ),
                excel_worker=None,
                use_excel_pool=False,
            )
            stats = await manager.stats()
            _assert(stats.get("job_tasks_healthy") is False, "finish failure was not surfaced in health stats")
            job_tasks = stats.get("job_tasks")
            _assert(isinstance(job_tasks, dict), "job task stats missing")
            _assert(job_tasks.get("failures") == 1, f"finish failure count was not tracked: {job_tasks}")
            _assert(proc_file.exists(), "reward upload was deleted after terminal DB update failed")
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_finish_noop_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        output_root.mkdir()
        proc_file = output_root / "late.xlsx"
        proc_file.write_bytes(b"placeholder")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            old_started_at = time.time() - 120.0
            late_job = JobRecord(
                job_id="late-finish",
                thread_dir="thread_1",
                gt_file=output_root / "target.xlsx",
                proc_file=proc_file,
                answer_position="Sheet1!A1",
                status=JobStatus.RUNNING,
                created_at_s=old_started_at,
                started_at_s=old_started_at,
            )
            _assert(store.enqueue(late_job, max_queue_size=10), "late finish job was not accepted")
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        ("seed-worker", late_job.job_id),
                    )
            store.mark_stale_running_as_error(older_than_s=1.0, msg="stale before late finish")
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            manager._run_sem = asyncio.Semaphore(1)

            async def fast_reward(job, *, excel_worker, use_excel_pool):
                return 1.0, "late"

            manager._compute_reward = fast_reward
            await manager._run_job(late_job, excel_worker=None, use_excel_pool=False)
            snapshot = store.get_snapshot("late-finish")
            _assert(snapshot is not None, "late finish job disappeared")
            _assert(snapshot.status is JobStatus.ERROR, f"late no-op finish overwrote stale row: {snapshot}")
            _assert(proc_file.exists(), "late no-op finish deleted reward upload")
            stats = await manager.stats()
            _assert(stats.get("job_tasks_healthy") is False, "late no-op finish was not surfaced")
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_finish_cancel_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        output_root.mkdir()
        proc_file = output_root / ".async_reward_jobs" / "output_finish-cancel.xlsx"
        proc_file.parent.mkdir()
        proc_file.write_bytes(b"placeholder")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_keep_files = os.environ.get("REWARD_API_KEEP_FILES")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_KEEP_FILES"] = "1"
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            job = JobRecord(
                job_id="finish-cancel",
                thread_dir="thread_1",
                gt_file=output_root / "target.xlsx",
                proc_file=proc_file,
                answer_position="Sheet1!A1",
                status=JobStatus.RUNNING,
                started_at_s=time.time(),
            )
            _assert(store.enqueue(job, max_queue_size=10), "finish-cancel job was not accepted")
            blocking_store = _BlockingFinishStore(store)
            manager = RewardJobManager(store=blocking_store, platform=Platform.WINDOWS)
            manager._run_sem = asyncio.Semaphore(1)
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        (manager._worker_id, job.job_id),
                    )

            async def fast_reward(job, *, excel_worker, use_excel_pool):
                return 1.0, "done"

            manager._compute_reward = fast_reward
            run_task = asyncio.create_task(manager._run_job(job, excel_worker=None, use_excel_pool=False))
            started = await asyncio.to_thread(blocking_store.done_started.wait, 5.0)
            _assert(started, "terminal finish did not start")
            run_task.cancel()
            await asyncio.sleep(0)
            run_task.cancel()
            await asyncio.sleep(0)
            blocking_store.release_done.set()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            snapshot = store.get_snapshot("finish-cancel")
            _assert(snapshot is not None, "finish-cancel job disappeared")
            _assert(snapshot.status is JobStatus.DONE, f"cancelled terminal finish changed status: {snapshot}")
            _assert(blocking_store.error_finishes == 0, "cancelled terminal finish wrote a competing error")
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_keep_files is None:
                os.environ.pop("REWARD_API_KEEP_FILES", None)
            else:
                os.environ["REWARD_API_KEEP_FILES"] = original_keep_files

    with temporary_directory(prefix="async_reward_api_immediate_cleanup_root_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        outside_root = tmp_path / "outside"
        output_root.mkdir()
        outside_root.mkdir()
        outside_file = outside_root / "outside.xlsx"
        sample_dir = output_root / "thread_1"
        job_dir = sample_dir / ".async_reward_jobs"
        target_file = sample_dir / "target.xlsx"
        valid_immediate_job_id = "valid-immediate-cleanup"
        valid_immediate_file = job_dir / f"output_{valid_immediate_job_id}.xlsx"
        valid_cancelled_job_id = "valid-cancelled-cleanup"
        valid_cancelled_file = job_dir / f"output_{valid_cancelled_job_id}.xlsx"
        job_dir.mkdir(parents=True)
        target_file.write_bytes(b"target")
        valid_immediate_file.write_bytes(b"valid immediate output")
        valid_cancelled_file.write_bytes(b"valid cancelled output")
        outside_file.write_bytes(b"placeholder")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_keep_files = os.environ.get("REWARD_API_KEEP_FILES")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_KEEP_FILES"] = "0"
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            manager._run_sem = asyncio.Semaphore(1)

            async def fast_reward(job, *, excel_worker, use_excel_pool):
                return 1.0, "fast"

            manager._compute_reward = fast_reward
            target_job = JobRecord(
                job_id="target-immediate-cleanup",
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=target_file,
                answer_position="Sheet1!A1",
                status=JobStatus.RUNNING,
                started_at_s=time.time(),
            )
            valid_immediate_job = JobRecord(
                job_id=valid_immediate_job_id,
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=valid_immediate_file,
                answer_position="Sheet1!A1",
                status=JobStatus.RUNNING,
                started_at_s=time.time(),
            )
            _assert(store.enqueue(target_job, max_queue_size=10), "target immediate job was not accepted")
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        (manager._worker_id, target_job.job_id),
                    )
            _assert(store.enqueue(valid_immediate_job, max_queue_size=10), "valid immediate job was not accepted")
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        (manager._worker_id, valid_immediate_job.job_id),
                    )
            await manager._run_job(target_job, excel_worker=None, use_excel_pool=False)
            await manager._run_job(valid_immediate_job, excel_worker=None, use_excel_pool=False)
            _assert(target_file.exists(), "immediate cleanup deleted target.xlsx through reward proc_file")
            _assert(not valid_immediate_file.exists(), "immediate cleanup did not delete a valid reward output")

            await manager._run_job(
                JobRecord(
                    job_id="outside-immediate-cleanup",
                    thread_dir="thread_1",
                    gt_file=target_file,
                    proc_file=outside_file,
                    answer_position="Sheet1!A1",
                    status=JobStatus.RUNNING,
                ),
                excel_worker=None,
                use_excel_pool=False,
            )
            _assert(outside_file.exists(), "immediate cleanup deleted an out-of-root reward file")

            async def cancelled_reward(job, *, excel_worker, use_excel_pool):
                raise asyncio.CancelledError()

            manager._compute_reward = cancelled_reward
            try:
                await manager._run_job(
                    JobRecord(
                        job_id=valid_cancelled_job_id,
                        thread_dir="thread_1",
                        gt_file=target_file,
                        proc_file=valid_cancelled_file,
                        answer_position="Sheet1!A1",
                        status=JobStatus.RUNNING,
                    ),
                    excel_worker=None,
                    use_excel_pool=False,
                )
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled cleanup suppressed cancellation")
            _assert(valid_cancelled_file.exists(), "cancelled cleanup deleted reward file after cancellation")
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_keep_files is None:
                os.environ.pop("REWARD_API_KEEP_FILES", None)
            else:
                os.environ["REWARD_API_KEEP_FILES"] = original_keep_files

    with temporary_directory(prefix="async_reward_api_claim_cancel_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        job = JobRecord(
            job_id="claim-cancel",
            thread_dir="thread_1",
            gt_file=tmp_path / "target.xlsx",
            proc_file=tmp_path / "output.xlsx",
            answer_position="Sheet1!A1",
        )
        _assert(store.enqueue(job, max_queue_size=10), "claim-cancel job was not accepted")

        original_claim_next = store.claim_next
        claim_started = threading.Event()
        claim_release = threading.Event()

        def blocking_claim_next(**kwargs):
            claim_started.set()
            claim_release.wait(timeout=5.0)
            return original_claim_next(**kwargs)

        store.claim_next = blocking_claim_next
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        manager._run_sem = asyncio.Semaphore(1)
        worker_loop_task = asyncio.create_task(manager._worker_loop())
        try:
            started = await asyncio.to_thread(claim_started.wait, 5.0)
            _assert(started, "worker loop did not start claim")
            worker_loop_task.cancel()
            claim_release.set()
            try:
                await worker_loop_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled worker loop did not raise CancelledError")
            snapshot = store.get_snapshot("claim-cancel")
            _assert(snapshot is not None, "claim-cancel job disappeared")
            _assert(snapshot.status is JobStatus.QUEUED, f"cancelled claim was not requeued: {snapshot}")
        finally:
            claim_release.set()
            if not worker_loop_task.done():
                worker_loop_task.cancel()
            await asyncio.gather(worker_loop_task, return_exceptions=True)
            await manager.shutdown()

    with temporary_directory(prefix="async_reward_api_claim_double_cancel_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        job = JobRecord(
            job_id="claim-double-cancel",
            thread_dir="thread_1",
            gt_file=tmp_path / "target.xlsx",
            proc_file=tmp_path / "output.xlsx",
            answer_position="Sheet1!A1",
        )
        _assert(store.enqueue(job, max_queue_size=10), "claim-double-cancel job was not accepted")

        original_claim_next = store.claim_next
        original_requeue_claimed = store.requeue_claimed
        claim_started = threading.Event()
        claim_release = threading.Event()
        requeue_started = threading.Event()
        requeue_release = threading.Event()

        def blocking_claim_next(**kwargs):
            claim_started.set()
            claim_release.wait(timeout=5.0)
            return original_claim_next(**kwargs)

        def blocking_requeue_claimed(**kwargs):
            requeue_started.set()
            requeue_release.wait(timeout=5.0)
            return original_requeue_claimed(**kwargs)

        store.claim_next = blocking_claim_next
        store.requeue_claimed = blocking_requeue_claimed
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        manager._run_sem = asyncio.Semaphore(1)
        worker_loop_task = asyncio.create_task(manager._worker_loop())
        try:
            started = await asyncio.to_thread(claim_started.wait, 5.0)
            _assert(started, "worker loop did not start double-cancel claim")
            worker_loop_task.cancel()
            claim_release.set()
            requeue_reached = await asyncio.to_thread(requeue_started.wait, 5.0)
            _assert(requeue_reached, "worker loop did not start double-cancel requeue")
            worker_loop_task.cancel()
            await asyncio.sleep(0)
            requeue_release.set()
            try:
                await worker_loop_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("double-cancelled worker loop did not raise CancelledError")
            snapshot = store.get_snapshot("claim-double-cancel")
            _assert(snapshot is not None, "claim-double-cancel job disappeared")
            _assert(snapshot.status is JobStatus.QUEUED, f"double-cancelled claim was not requeued: {snapshot}")
        finally:
            claim_release.set()
            requeue_release.set()
            if not worker_loop_task.done():
                worker_loop_task.cancel()
            await asyncio.gather(worker_loop_task, return_exceptions=True)
            await manager.shutdown()

    with temporary_directory(prefix="async_reward_api_claim_cancel_requeue_fail_") as tmp:
        tmp_path = Path(tmp)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        job = JobRecord(
            job_id="claim-cancel-requeue-fail",
            thread_dir="thread_1",
            gt_file=tmp_path / "target.xlsx",
            proc_file=tmp_path / "output.xlsx",
            answer_position="Sheet1!A1",
        )
        _assert(store.enqueue(job, max_queue_size=10), "claim-cancel requeue-fail job was not accepted")

        original_claim_next = store.claim_next
        claim_started = threading.Event()
        claim_release = threading.Event()

        def blocking_claim_next(**kwargs):
            claim_started.set()
            claim_release.wait(timeout=5.0)
            return original_claim_next(**kwargs)

        def failing_requeue_claimed(**kwargs):
            raise RuntimeError("requeue failed")

        store.claim_next = blocking_claim_next
        store.requeue_claimed = failing_requeue_claimed
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        manager._run_sem = asyncio.Semaphore(1)
        worker_loop_task = asyncio.create_task(manager._worker_loop())
        try:
            started = await asyncio.to_thread(claim_started.wait, 5.0)
            _assert(started, "worker loop did not start requeue-fail claim")
            worker_loop_task.cancel()
            claim_release.set()
            try:
                await worker_loop_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled requeue-fail worker loop did not raise CancelledError")
            _assert(manager._run_sem._value == 1, "requeue failure leaked the run semaphore")
            _assert(manager._job_task_failures == 1, "requeue failure was not recorded")
        finally:
            claim_release.set()
            if not worker_loop_task.done():
                worker_loop_task.cancel()
            await asyncio.gather(worker_loop_task, return_exceptions=True)
            await manager.shutdown()

    with temporary_directory(prefix="async_reward_api_cleanup_paths_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        outside_root = tmp_path / "outside"
        output_root.mkdir()
        outside_root.mkdir()
        outside_file = outside_root / "sensitive.xlsx"
        sample_dir = output_root / "thread_1"
        job_dir = sample_dir / ".async_reward_jobs"
        target_file = sample_dir / "target.xlsx"
        valid_reward_job_id = "valid-reward-cleanup"
        valid_reward_file = job_dir / f"output_{valid_reward_job_id}.xlsx"
        quarantined_job_id = "quarantined-invalid-cleanup"
        quarantined_file = outside_root / "quarantined.xlsx"
        job_dir.mkdir(parents=True)
        target_file.write_bytes(b"target")
        valid_reward_file.write_bytes(b"valid reward output")
        malformed_proc_file = Path(str(output_root / "malformed.xlsx") + "\x00")
        outside_file.write_bytes(b"placeholder")
        quarantined_file.write_bytes(b"quarantined")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_keep_files = os.environ.get("REWARD_API_KEEP_FILES")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_KEEP_FILES"] = "0"
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            old_finished_at = time.time() - 7200.0
            bad_job = JobRecord(
                job_id="outside-cleanup",
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=outside_file,
                answer_position="Sheet1!A1",
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=1.0,
            )
            malformed_job = JobRecord(
                job_id="malformed-reward-cleanup",
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=malformed_proc_file,
                answer_position="Sheet1!A1",
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=1.0,
            )
            empty_proc_job = JobRecord(
                job_id="empty-proc-cleanup",
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=output_root / "empty.xlsx",
                answer_position="Sheet1!A1",
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=1.0,
            )
            target_proc_job = JobRecord(
                job_id="target-proc-cleanup",
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=target_file,
                answer_position="Sheet1!A1",
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=1.0,
            )
            valid_reward_job = JobRecord(
                job_id=valid_reward_job_id,
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=valid_reward_file,
                answer_position="Sheet1!A1",
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=1.0,
            )
            quarantined_invalid_job = JobRecord(
                job_id=quarantined_job_id,
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=quarantined_file,
                answer_position="Sheet1!A1",
                status=JobStatus.ERROR,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
                msg="invalid persisted job row: proc_file=invalid",
            )
            _assert(store.enqueue(bad_job, max_queue_size=10), "outside cleanup job was not accepted")
            _assert(
                store.enqueue(malformed_job, max_queue_size=10),
                "malformed reward cleanup job was not accepted",
            )
            _assert(store.enqueue(empty_proc_job, max_queue_size=10), "empty proc cleanup job was not accepted")
            _assert(store.enqueue(target_proc_job, max_queue_size=10), "target-proc cleanup job was not accepted")
            _assert(store.enqueue(valid_reward_job, max_queue_size=10), "valid reward cleanup job was not accepted")
            _assert(
                store.enqueue(quarantined_invalid_job, max_queue_size=10),
                "quarantined invalid cleanup job was not accepted",
            )
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET proc_file = ? WHERE job_id = ?;",
                        ("", "empty-proc-cleanup"),
                    )
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            await manager._cleanup_finished_jobs_once(pass_now_s=time.time())
            _assert(outside_file.exists(), "cleanup deleted an out-of-root reward file")
            _assert(quarantined_file.exists(), "cleanup deleted quarantined invalid out-of-root file")
            _assert(target_file.exists(), "cleanup deleted target.xlsx through reward proc_file")
            _assert(not valid_reward_file.exists(), "cleanup did not delete a valid reward output")
            _assert(store.get_snapshot("outside-cleanup") is not None, "out-of-root cleanup row was deleted")
            _assert(
                store.get_snapshot(quarantined_job_id) is None,
                "quarantined invalid cleanup row was retried instead of deleted",
            )
            _assert(
                store.get_snapshot("malformed-reward-cleanup") is None,
                "malformed reward cleanup row was retried instead of deleted",
            )
            _assert(store.get_snapshot("target-proc-cleanup") is not None, "target proc cleanup row was deleted")
            _assert(store.get_snapshot(valid_reward_job_id) is None, "valid reward cleanup row was not deleted")
            _assert(store.get_snapshot("empty-proc-cleanup") is None, "empty proc cleanup row was not deleted")
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_keep_files is None:
                os.environ.pop("REWARD_API_KEEP_FILES", None)
            else:
                os.environ["REWARD_API_KEEP_FILES"] = original_keep_files

    with temporary_directory(prefix="async_reward_api_recalc_cleanup_shape_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        invalid_dir = recalc_root / "important"
        invalid_file = invalid_dir / "workbook.xlsx"
        separator_job_id = "nested/important"
        separator_dir = recalc_root / "nested" / "important"
        separator_file = separator_dir / "workbook.xlsx"
        nul_job_id = "bad\x00id"
        nul_dir = recalc_root / "bad-nul-id"
        nul_file = nul_dir / "workbook.xlsx"
        malformed_proc_job_id = "abcdef0123456789abcdef0123456789"
        malformed_proc_file = Path(
            str(recalc_root / malformed_proc_job_id / "workbook.xlsx") + "\x00"
        )
        valid_job_id = "0123456789abcdef0123456789abcdef"
        valid_dir = recalc_root / valid_job_id
        valid_file = valid_dir / "workbook.xlsx"
        current_root_job_id = "11111111111111111111111111111111"
        current_root_dir = (tmp_path / "changed_recalc_root") / current_root_job_id
        current_root_file = current_root_dir / "workbook.xlsx"
        outside_root_job_id = "00112233445566778899aabbccddeeff"
        outside_root_dir = tmp_path / "outside_recalc_root" / outside_root_job_id
        outside_root_file = outside_root_dir / "workbook.xlsx"
        stored_root_job_id = "fedcba9876543210fedcba9876543210"
        stored_root_dir = recalc_root / stored_root_job_id
        stored_root_file = stored_root_dir / "workbook.xlsx"
        invalid_dir.mkdir(parents=True)
        separator_dir.mkdir(parents=True)
        nul_dir.mkdir(parents=True)
        valid_dir.mkdir(parents=True)
        current_root_dir.mkdir(parents=True)
        outside_root_dir.mkdir(parents=True)
        stored_root_dir.mkdir(parents=True)
        invalid_file.write_bytes(b"important")
        separator_file.write_bytes(b"nested")
        nul_file.write_bytes(b"nul")
        valid_file.write_bytes(b"placeholder")
        current_root_file.write_bytes(b"current root")
        outside_root_file.write_bytes(b"outside root")
        stored_root_file.write_bytes(b"stored root")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_keep_files = os.environ.get("REWARD_API_KEEP_FILES")
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        os.environ["REWARD_API_KEEP_FILES"] = "0"
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            old_finished_at = time.time() - 7200.0
            invalid_job = JobRecord(
                job_id="invalid-recalc-cleanup",
                thread_dir="recalculate",
                gt_file=invalid_file,
                proc_file=invalid_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            separator_job = JobRecord(
                job_id=separator_job_id,
                thread_dir="recalculate",
                gt_file=separator_file,
                proc_file=separator_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            nul_job = JobRecord(
                job_id=nul_job_id,
                thread_dir="recalculate",
                gt_file=nul_file,
                proc_file=nul_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            malformed_proc_job = JobRecord(
                job_id=malformed_proc_job_id,
                thread_dir="recalculate",
                gt_file=valid_file,
                proc_file=malformed_proc_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            valid_job = JobRecord(
                job_id=valid_job_id,
                thread_dir="recalculate",
                gt_file=valid_file,
                proc_file=valid_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            current_root_job = JobRecord(
                job_id=current_root_job_id,
                thread_dir="recalculate",
                gt_file=current_root_dir.parent,
                proc_file=current_root_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            outside_root_job = JobRecord(
                job_id=outside_root_job_id,
                thread_dir="recalculate",
                gt_file=outside_root_dir.parent,
                proc_file=outside_root_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            stored_root_job = JobRecord(
                job_id=stored_root_job_id,
                thread_dir="recalculate",
                gt_file=recalc_root,
                proc_file=stored_root_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.DONE,
                created_at_s=old_finished_at,
                finished_at_s=old_finished_at,
                reward=0.0,
            )
            _assert(store.enqueue(invalid_job, max_queue_size=10), "invalid-shape recalc job was not accepted")
            _assert(store.enqueue(separator_job, max_queue_size=10), "separator recalc job was not accepted")
            _assert(store.enqueue(nul_job, max_queue_size=10), "nul recalc job was not accepted")
            _assert(
                store.enqueue(malformed_proc_job, max_queue_size=10),
                "malformed-proc recalc job was not accepted",
            )
            _assert(store.enqueue(valid_job, max_queue_size=10), "valid-shape recalc job was not accepted")
            _assert(store.enqueue(current_root_job, max_queue_size=10), "current-root recalc job was not accepted")
            _assert(store.enqueue(outside_root_job, max_queue_size=10), "outside-root recalc job was not accepted")
            _assert(store.enqueue(stored_root_job, max_queue_size=10), "stored-root recalc job was not accepted")
            os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "changed_recalc_root")
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            await manager._cleanup_finished_jobs_once(pass_now_s=time.time())
            _assert(invalid_file.exists(), "cleanup deleted an unexpected recalc result path")
            _assert(invalid_dir.exists(), "cleanup deleted an unexpected recalc directory")
            _assert(separator_file.exists(), "cleanup accepted a recalc job id containing path separators")
            _assert(nul_file.exists(), "cleanup accepted a recalc job id containing an embedded NUL")
            _assert(valid_file.exists(), "cleanup should refuse old-root recalc paths after root drift")
            _assert(outside_root_file.exists(), "cleanup deleted a recalc result outside the configured root")
            _assert(outside_root_dir.exists(), "cleanup deleted an outside-root recalc directory")
            _assert(stored_root_dir.exists(), "cleanup should refuse old-root recalc paths after root drift")
            _assert(
                store.get_snapshot("invalid-recalc-cleanup") is not None,
                "unexpected recalc cleanup row was deleted",
            )
            _assert(
                store.get_snapshot(separator_job_id) is not None,
                "separator recalc cleanup row was deleted",
            )
            _assert(
                store.get_snapshot(nul_job_id) is None,
                "nul recalc cleanup row was retried instead of deleted",
            )
            _assert(
                store.get_snapshot(malformed_proc_job_id) is None,
                "malformed-proc recalc cleanup row was retried instead of deleted",
            )
            _assert(
                store.get_snapshot(valid_job_id) is not None,
                "old-root recalc cleanup row was deleted after root drift",
            )
            _assert(
                store.get_snapshot(outside_root_job_id) is not None,
                "outside-root recalc cleanup row was deleted",
            )
            _assert(
                store.get_snapshot(stored_root_job_id) is not None,
                "stored-root recalc cleanup row should remain after root drift",
            )
            _assert(
                not current_root_dir.exists(),
                "cleanup did not remove the exact recalc job directory under the configured root",
            )
            _assert(
                store.get_snapshot(current_root_job_id) is None,
                "cleanup did not delete the exact recalc job row under the configured root",
            )
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root
            if original_keep_files is None:
                os.environ.pop("REWARD_API_KEEP_FILES", None)
            else:
                os.environ["REWARD_API_KEEP_FILES"] = original_keep_files

    with temporary_directory(prefix="async_reward_api_run_job_path_validation_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        recalc_root = tmp_path / "recalc_root"
        outside_root = tmp_path / "outside"
        output_root.mkdir()
        recalc_root.mkdir()
        outside_root.mkdir()
        sample_dir = output_root / "thread_1"
        job_dir = sample_dir / ".async_reward_jobs"
        job_dir.mkdir(parents=True)
        target_file = sample_dir / "target.xlsx"
        target_file.write_bytes(b"target")
        unsafe_reward_file = outside_root / "unsafe_reward.xlsx"
        unsafe_reward_file.write_bytes(b"unsafe reward")
        unsafe_recalc_file = outside_root / "unsafe_recalc.xlsx"
        unsafe_recalc_file.write_bytes(b"unsafe recalc")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)

            class _PathValidationPool:
                def __init__(self) -> None:
                    self._available: asyncio.Queue[object] = asyncio.Queue()

                async def release(self, worker) -> None:
                    await self._available.put(worker)

            now = time.time()
            reward_job = JobRecord(
                job_id="unsafe-reward-run",
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=unsafe_reward_file,
                answer_position="Sheet1!A1",
                status=JobStatus.RUNNING,
                created_at_s=now,
                started_at_s=now,
            )
            _assert(store.enqueue(reward_job, max_queue_size=10), "unsafe reward run job was not accepted")
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        (manager._worker_id, reward_job.job_id),
                    )

            async def fail_reward(job, *, excel_worker, use_excel_pool):
                raise AssertionError("unsafe persisted reward path reached worker")

            manager._compute_reward = fail_reward
            path_validation_pool = _PathValidationPool()
            path_validation_worker = object()
            manager._excel_pool = path_validation_pool
            await manager._run_job(reward_job, excel_worker=path_validation_worker, use_excel_pool=True)
            _assert(path_validation_pool._available.qsize() == 1, "pooled invalid-path worker was not released")
            _assert(
                path_validation_pool._available.get_nowait() is path_validation_worker,
                "pooled invalid-path release returned the wrong worker",
            )
            reward_snapshot = store.get_snapshot(reward_job.job_id)
            if reward_snapshot is None:
                raise AssertionError("unsafe reward run snapshot disappeared")
            _assert(reward_snapshot.status is JobStatus.ERROR, f"unsafe reward run status: {reward_snapshot}")
            _assert(
                reward_snapshot.msg == "invalid persisted job path",
                f"unsafe reward run diagnostic changed: {reward_snapshot.msg!r}",
            )
            _assert(unsafe_reward_file.exists(), "unsafe reward run deleted outside file")

            recalc_job_id = "abcdef0123456789abcdef0123456789"
            recalc_job = JobRecord(
                job_id=recalc_job_id,
                thread_dir="recalculate",
                gt_file=unsafe_recalc_file,
                proc_file=unsafe_recalc_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.RUNNING,
                created_at_s=now,
                started_at_s=now,
            )
            _assert(store.enqueue(recalc_job, max_queue_size=10), "unsafe recalc run job was not accepted")
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        (manager._worker_id, recalc_job.job_id),
                    )

            async def fail_recalc(job, *, excel_worker, use_excel_pool):
                raise AssertionError("unsafe persisted recalc path reached worker")

            manager._recalc_job = fail_recalc
            await manager._run_job(recalc_job, excel_worker=None, use_excel_pool=False)
            recalc_snapshot = store.get_snapshot(recalc_job.job_id)
            if recalc_snapshot is None:
                raise AssertionError("unsafe recalc run snapshot disappeared")
            _assert(recalc_snapshot.status is JobStatus.ERROR, f"unsafe recalc run status: {recalc_snapshot}")
            _assert(
                recalc_snapshot.msg == "invalid persisted job path",
                f"unsafe recalc run diagnostic changed: {recalc_snapshot.msg!r}",
            )
            _assert(unsafe_recalc_file.exists(), "unsafe recalc run deleted outside file")

            stored_recalc_job_id = "fedcba9876543210fedcba9876543210"
            stored_recalc_dir = recalc_root / stored_recalc_job_id
            stored_recalc_file = stored_recalc_dir / "workbook.xlsx"
            stored_recalc_dir.mkdir(parents=True)
            stored_recalc_file.write_bytes(b"stored root recalc")
            stored_recalc_job = JobRecord(
                job_id=stored_recalc_job_id,
                thread_dir="recalculate",
                gt_file=recalc_root,
                proc_file=stored_recalc_file,
                answer_position="",
                kind=JobKind.RECALCULATE,
                status=JobStatus.RUNNING,
                created_at_s=now,
                started_at_s=now,
            )
            _assert(
                store.enqueue(stored_recalc_job, max_queue_size=10),
                "stored-root recalc run job was not accepted",
            )
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        (manager._worker_id, stored_recalc_job.job_id),
                    )
            os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "changed_recalc_root")
            recalc_calls: list[str] = []

            async def complete_recalc(job, *, excel_worker, use_excel_pool):
                recalc_calls.append(job.job_id)
                return "recalculated"

            manager._recalc_job = complete_recalc
            await manager._run_job(stored_recalc_job, excel_worker=None, use_excel_pool=False)
            stored_recalc_snapshot = store.get_snapshot(stored_recalc_job.job_id)
            if stored_recalc_snapshot is None:
                raise AssertionError("stored-root recalc run snapshot disappeared")
            _assert(
                stored_recalc_snapshot.status is JobStatus.ERROR,
                f"stored-root recalc run status: {stored_recalc_snapshot}",
            )
            _assert(
                stored_recalc_snapshot.msg == "invalid persisted job path",
                f"stored-root recalc run diagnostic changed: {stored_recalc_snapshot.msg!r}",
            )
            _assert(
                recalc_calls == [],
                f"stored-root recalc run reached worker after root drift: {recalc_calls}",
            )
        finally:
            if manager is not None:
                manager._excel_pool = None
                await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_keep_files_cleanup_") as tmp:
        tmp_path = Path(tmp)
        original_keep_files = os.environ.get("REWARD_API_KEEP_FILES")
        original_batch_size = os.environ.get("REWARD_API_CLEANUP_BATCH_SIZE")
        original_max_batches = os.environ.get("REWARD_API_CLEANUP_MAX_BATCHES")
        os.environ["REWARD_API_KEEP_FILES"] = "1"
        os.environ["REWARD_API_CLEANUP_BATCH_SIZE"] = "2"
        os.environ["REWARD_API_CLEANUP_MAX_BATCHES"] = "1"
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            old_finished_at = time.time() - 7200.0
            files: list[Path] = []
            for idx in range(5):
                proc_file = tmp_path / f"done_{idx}.xlsx"
                proc_file.write_bytes(b"placeholder")
                files.append(proc_file)
                job = JobRecord(
                    job_id=f"keep-files-{idx}",
                    thread_dir="thread_1",
                    gt_file=tmp_path / "target.xlsx",
                    proc_file=proc_file,
                    answer_position="Sheet1!A1",
                    status=JobStatus.DONE,
                    created_at_s=old_finished_at,
                    finished_at_s=old_finished_at,
                    reward=1.0,
                )
                _assert(store.enqueue(job, max_queue_size=10), f"keep-files job {idx} was not accepted")
            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            await manager._cleanup_finished_jobs_once(pass_now_s=time.time())
            _assert(store.stats()["jobs"] == 3, f"keep-files cleanup ignored batch size: {store.stats()}")
            _assert(all(path.exists() for path in files), "keep-files cleanup deleted workbook files")
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_keep_files is None:
                os.environ.pop("REWARD_API_KEEP_FILES", None)
            else:
                os.environ["REWARD_API_KEEP_FILES"] = original_keep_files
            if original_batch_size is None:
                os.environ.pop("REWARD_API_CLEANUP_BATCH_SIZE", None)
            else:
                os.environ["REWARD_API_CLEANUP_BATCH_SIZE"] = original_batch_size
            if original_max_batches is None:
                os.environ.pop("REWARD_API_CLEANUP_MAX_BATCHES", None)
            else:
                os.environ["REWARD_API_CLEANUP_MAX_BATCHES"] = original_max_batches

    with temporary_directory(prefix="async_reward_api_completion_wake_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        job_dir = sample_dir / ".async_reward_jobs"
        job_dir.mkdir(parents=True)
        target_file = sample_dir / "target.xlsx"
        target_file.write_bytes(b"target")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        manager = None
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            for job_id in ("wake-cap-1", "wake-cap-2"):
                job = JobRecord(
                    job_id=job_id,
                    thread_dir="thread_1",
                    gt_file=target_file,
                    proc_file=job_dir / f"output_{job_id}.xlsx",
                    answer_position="Sheet1!A1",
                )
                _assert(store.enqueue(job, max_queue_size=10), f"{job_id} was not accepted")

            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            manager._run_sem = asyncio.Semaphore(2)
            manager._max_running_jobs = 1
            manager._poll_interval_s = 30.0
            manager._idle_poll_max_s = 30.0
            first_started = asyncio.Event()
            first_release = asyncio.Event()
            second_started = asyncio.Event()

            async def staged_reward(job, *, excel_worker, use_excel_pool):
                if job.job_id == "wake-cap-1":
                    first_started.set()
                    await first_release.wait()
                if job.job_id == "wake-cap-2":
                    second_started.set()
                return 1.0, "fast"

            manager._compute_reward = staged_reward
            worker_loop_task = asyncio.create_task(manager._worker_loop())
            await asyncio.wait_for(first_started.wait(), timeout=2.0)
            await asyncio.sleep(0.1)
            first_release.set()
            await asyncio.wait_for(second_started.wait(), timeout=2.0)
            manager._stop.set()
            worker_loop_task.cancel()
            try:
                await worker_loop_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("completion wake worker loop did not cancel")
            pending_job_tasks = list(manager._job_tasks)
            if pending_job_tasks:
                await asyncio.wait_for(asyncio.gather(*pending_job_tasks), timeout=2.0)
        finally:
            if manager is not None:
                await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_per_job_concurrency_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        job_dir = sample_dir / ".async_reward_jobs"
        job_dir.mkdir(parents=True)
        target_file = sample_dir / "target.xlsx"
        target_file.write_bytes(b"target")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_instance_per_worker = os.environ.get("REWARD_API_INSTANCE_PER_WORKER")
        original_max_running_jobs = os.environ.get("REWARD_API_MAX_RUNNING_JOBS")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_INSTANCE_PER_WORKER"] = "0"
        os.environ["REWARD_API_MAX_RUNNING_JOBS"] = "2"
        try:
            store = SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            for job_id in ("per-job-1", "per-job-2"):
                job = JobRecord(
                    job_id=job_id,
                    thread_dir="thread_1",
                    gt_file=target_file,
                    proc_file=job_dir / f"output_{job_id}.xlsx",
                    answer_position="Sheet1!A1",
                )
                _assert(store.enqueue(job, max_queue_size=10), f"{job_id} was not accepted")

            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            started: set[str] = set()
            all_started = asyncio.Event()
            release = asyncio.Event()

            async def blocking_reward(job, *, excel_worker, use_excel_pool):
                started.add(job.job_id)
                if len(started) == 2:
                    all_started.set()
                await release.wait()
                return 1.0, "done"

            manager._compute_reward = blocking_reward
            await manager.start()
            try:
                stats = await manager.stats()
                _assert(stats.get("concurrency") == 2, f"per-job concurrency was not honored: {stats}")
                await asyncio.wait_for(all_started.wait(), timeout=3.0)
                snapshots = [await manager.get_snapshot(job_id) for job_id in ("per-job-1", "per-job-2")]
                _assert(
                    all(snapshot is not None and snapshot.status is JobStatus.RUNNING for snapshot in snapshots),
                    f"per-job jobs did not run concurrently: {snapshots}",
                )
                release.set()
            finally:
                release.set()
                await manager.shutdown()
        finally:
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_instance_per_worker is None:
                os.environ.pop("REWARD_API_INSTANCE_PER_WORKER", None)
            else:
                os.environ["REWARD_API_INSTANCE_PER_WORKER"] = original_instance_per_worker
            if original_max_running_jobs is None:
                os.environ.pop("REWARD_API_MAX_RUNNING_JOBS", None)
            else:
                os.environ["REWARD_API_MAX_RUNNING_JOBS"] = original_max_running_jobs

    with temporary_directory(prefix="async_reward_api_submit_wake_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        job_dir = sample_dir / ".async_reward_jobs"
        job_dir.mkdir(parents=True)
        target_file = sample_dir / "target.xlsx"
        target_file.write_bytes(b"target")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        store = SqliteJobStore(tmp_path / "jobs.sqlite3")
        manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
        manager._poll_interval_s = 30.0
        manager._idle_poll_max_s = 30.0

        async def fast_reward(job, *, excel_worker, use_excel_pool):
            return 1.0, "fast"

        manager._compute_reward = fast_reward
        await manager.start()
        try:
            await asyncio.sleep(0.1)
            job = JobRecord(
                job_id="wake-job",
                thread_dir="thread_1",
                gt_file=target_file,
                proc_file=job_dir / "output_wake-job.xlsx",
                answer_position="Sheet1!A1",
            )
            started = time.monotonic()
            accepted = await manager.submit(job)
            _assert(accepted, "submit wake test job was not accepted")
            snapshot = None
            while time.monotonic() - started < 2.0:
                snapshot = await manager.get_snapshot(job.job_id)
                if snapshot is not None and snapshot.status is JobStatus.DONE:
                    break
                await asyncio.sleep(0.02)
            _assert(snapshot is not None, "submit wake test job disappeared")
            _assert(snapshot.status is JobStatus.DONE, f"submit wake did not run job promptly: {snapshot}")
            _assert(time.monotonic() - started < 2.0, "submit wake waited for idle poll timeout")
        finally:
            await manager.shutdown()
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    print("OK: manager startup recovery looks good")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
