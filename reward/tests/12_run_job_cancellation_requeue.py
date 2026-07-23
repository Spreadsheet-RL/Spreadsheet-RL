from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from _tempdir import temporary_directory

os.environ["REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"] = "1"
os.environ["REWARD_API_PLATFORM"] = "windows"

from async_reward_api.excel_pool import ExcelWorkerPool  # noqa: E402
from async_reward_api.job_store import SqliteJobStore  # noqa: E402
from async_reward_api.main import RewardJobManager  # noqa: E402
from async_reward_api.models import JobKind, JobRecord, JobStatus  # noqa: E402
from async_reward_api.platform import Platform  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _QuiesceOrderStore(SqliteJobStore):
    def __init__(self, db_path: Path, *, shutdown_completed: threading.Event) -> None:
        super().__init__(db_path)
        self._shutdown_completed = shutdown_completed
        self.requeue_saw_shutdown_complete: bool | None = None

    def requeue_claimed(self, *, job_id: str, worker_id: str) -> bool:
        self.requeue_saw_shutdown_complete = self._shutdown_completed.is_set()
        return super().requeue_claimed(job_id=job_id, worker_id=worker_id)


class _BlockingPooledWorker:
    jobs_run = 0
    excel_pid = None
    is_running = True

    def __init__(self, *, shutdown_completed: threading.Event) -> None:
        self.run_started = asyncio.Event()
        self.shutdown_completed = shutdown_completed
        self.shutdown_force_calls: list[bool] = []

    async def run_job(self, **kwargs):
        self.run_started.set()
        await asyncio.Event().wait()

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_force_calls.append(force)
        await asyncio.sleep(0.05)
        self.is_running = False
        self.shutdown_completed.set()


async def main_async() -> int:
    original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
    try:
        with temporary_directory(prefix="async_reward_api_cancel_requeue_") as tmp:
            root = Path(tmp)
            output_root = root / "output_root"
            sample_dir = output_root / "thread_1"
            job_dir = sample_dir / ".async_reward_jobs"
            job_dir.mkdir(parents=True)
            gt_file = sample_dir / "target.xlsx"
            gt_file.write_bytes(b"target")

            job_id = "cancel-requeue"
            proc_file = job_dir / f"output_{job_id}.xlsx"
            proc_file.write_bytes(b"upload")
            os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)

            store = SqliteJobStore(root / "jobs.sqlite3")
            store.init()
            job = JobRecord(
                job_id=job_id,
                thread_dir="thread_1",
                gt_file=gt_file,
                proc_file=proc_file,
                answer_position="Sheet1!A1",
                kind=JobKind.REWARD,
                status=JobStatus.QUEUED,
                created_at_s=time.time(),
            )
            _assert(store.enqueue(job, max_queue_size=10), "queued job was not accepted")

            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            try:
                claimed = store.claim_next(worker_id=manager._worker_id, max_running_jobs=None)
                _assert(claimed is not None, "queued job was not claimed")

                async def cancelled_reward(*args, **kwargs):
                    raise asyncio.CancelledError()

                manager._compute_reward = cancelled_reward  # type: ignore[method-assign]
                try:
                    await manager._run_job(claimed, excel_worker=None, use_excel_pool=False)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancelled _run_job did not propagate cancellation")

                snapshot = store.get_snapshot(job_id)
                _assert(snapshot is not None, "cancelled job disappeared")
                _assert(snapshot.status is JobStatus.QUEUED, f"cancelled job was not requeued: {snapshot}")
                _assert(snapshot.started_at_s is None, f"requeued job kept started_at_s: {snapshot}")
                _assert(proc_file.exists(), "requeued job upload was deleted")
                with closing(sqlite3.connect(store.db_path)) as conn:
                    worker_id = conn.execute(
                        "SELECT worker_id FROM jobs WHERE job_id = ?;",
                        (job_id,),
                    ).fetchone()[0]
                _assert(worker_id is None, f"requeued job kept worker_id: {worker_id!r}")
            finally:
                await manager.shutdown()

        with temporary_directory(prefix="async_reward_api_pooled_cancel_quiesce_") as tmp:
            root = Path(tmp)
            output_root = root / "output_root"
            sample_dir = output_root / "thread_1"
            job_dir = sample_dir / ".async_reward_jobs"
            job_dir.mkdir(parents=True)
            gt_file = sample_dir / "target.xlsx"
            gt_file.write_bytes(b"target")

            job_id = "pooled-cancel-quiesce"
            proc_file = job_dir / f"output_{job_id}.xlsx"
            proc_file.write_bytes(b"upload")
            os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)

            shutdown_completed = threading.Event()
            store = _QuiesceOrderStore(root / "jobs.sqlite3", shutdown_completed=shutdown_completed)
            store.init()
            job = JobRecord(
                job_id=job_id,
                thread_dir="thread_1",
                gt_file=gt_file,
                proc_file=proc_file,
                answer_position="Sheet1!A1",
                kind=JobKind.REWARD,
                status=JobStatus.QUEUED,
                created_at_s=time.time(),
            )
            _assert(store.enqueue(job, max_queue_size=10), "pooled cancel job was not accepted")

            manager = RewardJobManager(store=store, platform=Platform.WINDOWS)
            task: asyncio.Task | None = None
            try:
                claimed = store.claim_next(worker_id=manager._worker_id, max_running_jobs=None)
                _assert(claimed is not None, "pooled cancel job was not claimed")

                worker = _BlockingPooledWorker(shutdown_completed=shutdown_completed)
                pool = ExcelWorkerPool(size=1, platform="windows")
                pool._workers = [worker]
                restart_calls: list[tuple[int, object]] = []

                def record_restart(idx: int, *, expected) -> None:
                    restart_calls.append((idx, expected))

                pool._schedule_restart = record_restart
                manager._excel_pool = pool

                task = asyncio.create_task(manager._run_job(claimed, excel_worker=worker, use_excel_pool=True))
                await asyncio.wait_for(worker.run_started.wait(), timeout=1.0)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("pooled cancelled _run_job did not propagate cancellation")

                snapshot = store.get_snapshot(job_id)
                _assert(snapshot is not None, "pooled cancelled job disappeared")
                _assert(snapshot.status is JobStatus.QUEUED, f"pooled cancelled job was not requeued: {snapshot}")
                _assert(
                    store.requeue_saw_shutdown_complete is True,
                    "pooled job requeued before cancelled worker shutdown completed",
                )
                _assert(worker.shutdown_completed.is_set(), "pooled worker was not quiesced before requeue")
                _assert(worker.shutdown_force_calls == [True], f"pooled worker shutdown calls: {worker.shutdown_force_calls}")
                _assert(restart_calls == [(0, worker)], f"pooled cancelled worker restart was not scheduled: {restart_calls}")
            finally:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await manager.shutdown()
    finally:
        if original_output_root is None:
            os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
        else:
            os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
    print("OK: run job cancellation requeues in-flight jobs")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
