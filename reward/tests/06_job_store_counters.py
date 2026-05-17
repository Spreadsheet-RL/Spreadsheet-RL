from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from async_reward_api.job_store import SqliteJobStore
from async_reward_api.models import JobRecord, JobStatus


def _assert_equal(label: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{label}:\n  got={got!r}\n  expected={expected!r}")


def _job(job_id: str, root: Path, *, status: JobStatus = JobStatus.QUEUED) -> JobRecord:
    now = time.time()
    return JobRecord(
        job_id=job_id,
        thread_dir="thread_1",
        gt_file=root / "target.xlsx",
        proc_file=root / f"{job_id}.xlsx",
        answer_position="Sheet1!A1",
        status=status,
        started_at_s=now if status is JobStatus.RUNNING else None,
        finished_at_s=now if status in {JobStatus.DONE, JobStatus.ERROR} else None,
        reward=1.0 if status is JobStatus.DONE else None,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="async_reward_api_job_store_") as tmp:
        root = Path(tmp)
        store = SqliteJobStore(root / "jobs.sqlite3")
        store.init()

        for job in (
            _job("queued_1", root),
            _job("queued_2", root),
            _job("running_1", root, status=JobStatus.RUNNING),
        ):
            if not store.enqueue(job, max_queue_size=10):
                raise AssertionError(f"enqueue failed for {job.job_id}")

        _assert_equal("initial stats", store.stats(), {"queued": 2, "running": 1, "jobs": 3})
        _assert_equal("queue full preflight", store.has_queue_capacity(max_queue_size=2), False)
        _assert_equal("queue available preflight", store.has_queue_capacity(max_queue_size=3), True)
        _assert_equal(
            "running cap blocks claim",
            store.claim_next(worker_id="worker", max_running_jobs=1),
            None,
        )

        store.finish(job_id="running_1", status=JobStatus.DONE, reward=1.0, msg="")
        claimed = store.claim_next(worker_id="worker", max_running_jobs=1)
        if claimed is None:
            raise AssertionError("expected claim after running slot opened")
        snapshot = store.get_snapshot(claimed.job_id)
        if snapshot is None:
            raise AssertionError("expected snapshot for claimed job")
        _assert_equal("claimed status", snapshot.status, JobStatus.RUNNING)
        _assert_equal("stats after claim", store.stats(), {"queued": 1, "running": 1, "jobs": 3})

        store.finish(job_id=claimed.job_id, status=JobStatus.DONE, reward=1.0, msg="")
        _assert_equal("chunk delete count", store.delete_jobs(job_ids=[claimed.job_id, "missing"]), 1)
        _assert_equal("stats after delete", store.stats(), {"queued": 1, "running": 0, "jobs": 2})

        deleted = store.delete_finished_before(cutoff_s=time.time() + 1.0)
        _assert_equal("finished cleanup delete count", deleted, 1)
        _assert_equal("final stats", store.stats(), {"queued": 1, "running": 0, "jobs": 1})

        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM job_counters WHERE name IN ('running', 'total');")
            conn.execute("UPDATE job_counters SET value = 99 WHERE name = 'queued';")
        store.init()
        _assert_equal("counter migration resync", store.stats(), {"queued": 1, "running": 0, "jobs": 1})

        for job in (
            _job("fresh_done", root, status=JobStatus.DONE),
            _job("retry_done", root, status=JobStatus.DONE),
        ):
            if not store.enqueue(job, max_queue_size=10):
                raise AssertionError(f"enqueue failed for {job.job_id}")
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET cleanup_next_retry_s = ? WHERE job_id = 'retry_done';",
                (time.time() - 1.0,),
            )
        batch = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=2,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "cleanup batch retry/fresh order",
            [job_id for job_id, _, _ in batch],
            ["retry_done", "fresh_done"],
        )
        padded_job_ids = (
            ["fresh_done"]
            + [f"missing_{i}" for i in range(901)]
            + ["retry_done", "fresh_done"]
        )
        _assert_equal(
            "chunked cleanup failure marking",
            store.mark_cleanup_failed(
                job_ids=padded_job_ids,
                retry_after_s=1.0,
                retry_max_s=1.0,
            ),
            2,
        )

    print("OK: job store counters look good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
