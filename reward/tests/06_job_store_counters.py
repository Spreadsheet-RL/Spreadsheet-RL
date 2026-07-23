from __future__ import annotations

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path

from async_reward_api.job_store import (
    SqliteJobStore,
    _CLEANUP_RETENTION_SQL,
    _MAX_SQLITE_INTEGER,
    _PooledConnection,
    _cleanup_attempts_value_is_valid,
    _get_sqlite_timeout_s,
)
from async_reward_api.models import JobKind, JobRecord, JobStatus
from _tempdir import temporary_directory


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


def _set_worker_id(store: SqliteJobStore, job_id: str, worker_id: str) -> None:
    with closing(sqlite3.connect(store.db_path)) as conn:
        with conn:
            conn.execute(
                "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                (worker_id, job_id),
            )


def _test_null_job_id_queue_head_claim_repair(
    root: Path,
    *,
    force_legacy: bool,
    cleanup: ExitStack,
) -> None:
    label = "legacy" if force_legacy else "returning"
    store = SqliteJobStore(root / f"null_job_id_queue_head_{label}.sqlite3")
    cleanup.callback(store.close)
    store.init()
    corrupt_job_id = f"null-head-{label}"
    valid_job_id = f"after-null-head-{label}"
    if not store.enqueue(_job(corrupt_job_id, root), max_queue_size=10):
        raise AssertionError(f"enqueue failed for {corrupt_job_id}")
    if not store.enqueue(_job(valid_job_id, root), max_queue_size=10):
        raise AssertionError(f"enqueue failed for {valid_job_id}")
    with closing(sqlite3.connect(store.db_path)) as conn:
        with conn:
            corrupt_rowid = conn.execute(
                "SELECT rowid FROM jobs WHERE job_id = ?;",
                (corrupt_job_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE jobs SET job_id = NULL, created_at_s = ? WHERE rowid = ?;",
                (1000.0, corrupt_rowid),
            )
            conn.execute(
                "UPDATE jobs SET created_at_s = ? WHERE job_id = ?;",
                (1000.0, valid_job_id),
            )

    if force_legacy:
        def fail_returning(conn: sqlite3.Connection, *, now: float, worker_id: str):
            raise sqlite3.OperationalError("RETURNING unsupported")

        store._claim_next_returning = fail_returning  # type: ignore[method-assign]

    _assert_equal(
        f"null job_id queue head {label} claim should repair only",
        store.claim_next(worker_id="worker", max_running_jobs=None),
        None,
    )
    with closing(sqlite3.connect(store.db_path)) as conn:
        repaired_row = conn.execute(
            "SELECT job_id, status, msg FROM jobs WHERE rowid = ?;",
            (corrupt_rowid,),
        ).fetchone()
    if repaired_row is None:
        raise AssertionError(f"null job_id queue head {label} row disappeared")
    _assert_equal(f"null job_id queue head {label} repaired id", repaired_row[0], f"invalid-{corrupt_rowid}")
    _assert_equal(f"null job_id queue head {label} repaired status", repaired_row[1], JobStatus.ERROR.value)
    if "job_id=invalid" not in str(repaired_row[2]):
        raise AssertionError(f"null job_id queue head {label} diagnostic missing: {repaired_row[2]!r}")

    claimed = store.claim_next(worker_id="worker", max_running_jobs=None)
    if claimed is None:
        raise AssertionError(f"valid job behind null queue head was not claimed via {label}")
    _assert_equal(f"valid claim behind null queue head {label}", claimed.job_id, valid_job_id)


class _CountingQuarantineStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.quarantine_calls = 0

    def _quarantine_invalid_rows(self, conn: sqlite3.Connection, *, job_id: str | None = None) -> int:
        self.quarantine_calls += 1
        return super()._quarantine_invalid_rows(conn, job_id=job_id)


class _CountingRepairStore(SqliteJobStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.missing_job_id_repairs = 0

    def _repair_missing_job_ids(self, conn: sqlite3.Connection, *, now: float) -> int:
        self.missing_job_id_repairs += 1
        return super()._repair_missing_job_ids(conn, now=now)


class _LockedBeginConnection:
    def __init__(self) -> None:
        self.attempts = 0

    def execute(self, sql: str, parameters=(), /):
        self.attempts += 1
        raise sqlite3.OperationalError("database is locked")


class _ReconnectProbeStore:
    def __init__(self, conn: _LockedBeginConnection) -> None:
        self.conn = conn
        self.drop_calls = 0
        self.get_calls = 0

    def _drop_connection(self, conn: sqlite3.Connection) -> None:
        self.drop_calls += 1

    def _get_thread_connection(self):
        self.get_calls += 1
        return self.conn


def _test_closed_thread_local_begin_reconnects(root: Path, *, cleanup: ExitStack) -> None:
    store = SqliteJobStore(root / "closed_thread.sqlite3")
    cleanup.callback(store.close)
    store.init()
    with store._connection() as conn:
        stale_connection = conn.raw_connection
    stale_connection.close()
    if not store.enqueue(_job("closed_thread_healed", root), max_queue_size=10):
        raise AssertionError("closed thread-local connection did not heal on BEGIN")
    with store._connection() as conn:
        if conn.raw_connection is stale_connection:
            raise AssertionError("closed thread-local connection was not replaced")


def _test_nonclosed_begin_operational_error_is_not_retried() -> None:
    fake_conn = _LockedBeginConnection()
    fake_store = _ReconnectProbeStore(fake_conn)
    pooled = _PooledConnection(fake_store, fake_conn)
    try:
        pooled.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc):
            raise AssertionError(f"unexpected OperationalError propagated: {exc}") from exc
    else:
        raise AssertionError("non-closed OperationalError was swallowed")
    _assert_equal("locked BEGIN attempts", fake_conn.attempts, 1)
    _assert_equal("locked BEGIN drop calls", fake_store.drop_calls, 0)
    _assert_equal("locked BEGIN reconnect calls", fake_store.get_calls, 0)


def _test_closed_other_thread_connection_heals_on_first_use(root: Path, *, cleanup: ExitStack) -> None:
    store = SqliteJobStore(root / "closed_other_thread.sqlite3")
    cleanup.callback(store.close)
    store.init()
    with ThreadPoolExecutor(max_workers=1) as executor:
        _assert_equal(
            "thread-pool initial stats",
            executor.submit(store.stats).result(),
            {"queued": 0, "running": 0, "jobs": 0},
        )
        store.close()
        try:
            executor.submit(store.stats).result()
        except sqlite3.ProgrammingError as exc:
            if "closed" not in str(exc):
                raise AssertionError(f"unexpected closed-store error: {exc}") from exc
        else:
            raise AssertionError("closed store accepted thread-pool stats")
        store.init()
        _assert_equal(
            "thread-pool stats after reinit",
            executor.submit(store.stats).result(),
            {"queued": 0, "running": 0, "jobs": 0},
        )


def _test_temporary_directory_propagates_body_exceptions() -> None:
    try:
        with temporary_directory(prefix="async_reward_api_tempdir_body_error_"):
            raise RuntimeError("body failure")
    except RuntimeError as exc:
        if str(exc) != "body failure":
            raise AssertionError(f"unexpected temporary_directory body error: {exc!r}") from exc
    else:
        raise AssertionError("temporary_directory suppressed a body exception")


def main() -> int:
    _test_temporary_directory_propagates_body_exceptions()
    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_job_store_"))
        root = Path(tmp)
        _test_closed_thread_local_begin_reconnects(root, cleanup=cleanup)
        _test_nonclosed_begin_operational_error_is_not_retried()
        _test_closed_other_thread_connection_heals_on_first_use(root, cleanup=cleanup)
        store = SqliteJobStore(root / "jobs.sqlite3")
        cleanup.callback(store.close)
        store.init()

        for job in (
            _job("queued_1", root),
            _job("queued_2", root),
            _job("running_1", root, status=JobStatus.RUNNING),
        ):
            if not store.enqueue(job, max_queue_size=10):
                raise AssertionError(f"enqueue failed for {job.job_id}")
        _set_worker_id(store, "running_1", "seed-worker")

        _assert_equal("initial stats", store.stats(), {"queued": 2, "running": 1, "jobs": 3})
        _assert_equal("queue full preflight", store.has_queue_capacity(max_queue_size=2), False)
        _assert_equal("queue available preflight", store.has_queue_capacity(max_queue_size=3), True)
        _assert_equal("claimable jobs precheck", store.has_claimable_jobs(max_running_jobs=None), True)
        _assert_equal("running cap blocks claimable precheck", store.has_claimable_jobs(max_running_jobs=1), False)
        with store._connection() as conn:
            first_connection = conn.raw_connection
        store.stats()
        with store._connection() as conn:
            second_connection = conn.raw_connection
        if second_connection is not first_connection:
            raise AssertionError("thread-local sqlite connection was not reused")
        store.close()
        try:
            first_connection.execute("SELECT 1;")
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError("store.close did not close the cached sqlite connection")
        try:
            with store._connection():
                pass
        except sqlite3.ProgrammingError as exc:
            if "closed" not in str(exc):
                raise AssertionError(f"unexpected closed-store error: {exc}") from exc
        else:
            raise AssertionError("closed store reopened without init")
        store.init()
        with store._connection() as conn:
            reopened_connection = conn.raw_connection
        if reopened_connection is first_connection:
            raise AssertionError("store.close did not drop the cached sqlite connection")
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

        cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=10,
            retry_batch_share=0.5,
        )
        deleted = store.delete_jobs(job_ids=[job.job_id for job in cleanup_jobs])
        _assert_equal("finished cleanup delete count", deleted, 1)
        _assert_equal("final stats", store.stats(), {"queued": 1, "running": 0, "jobs": 1})

        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("DELETE FROM job_counters WHERE name IN ('running', 'total');")
                conn.execute("UPDATE job_counters SET value = 99 WHERE name = 'queued';")
        store.init()
        _assert_equal("counter migration resync", store.stats(), {"queued": 1, "running": 0, "jobs": 1})
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE job_counters SET value = ? WHERE name = ?;", (1.0e101, "queued"))
                conn.execute("UPDATE job_counters SET value = ? WHERE name = ?;", (-3, "running"))
                conn.execute("UPDATE job_counters SET value = ? WHERE name = ?;", ("bad", "total"))
        _assert_equal("corrupt counters repair", store.stats(), {"queued": 1, "running": 0, "jobs": 1})
        _assert_equal("queue capacity after counter repair", store.has_queue_capacity(max_queue_size=2), True)
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE job_counters SET value = ? WHERE name = ?;", (1.0e100, "queued"))
        _assert_equal(
            "sqlite-overflow-sized counter repair",
            store.stats(),
            {"queued": 1, "running": 0, "jobs": 1},
        )
        _assert_equal(
            "queue capacity after sqlite-overflow-sized counter repair",
            store.has_queue_capacity(max_queue_size=2),
            True,
        )
        original_sqlite_timeout = os.environ.get("REWARD_API_SQLITE_TIMEOUT_S")
        try:
            os.environ["REWARD_API_SQLITE_TIMEOUT_S"] = "0.25"
            _assert_equal("sqlite timeout override", _get_sqlite_timeout_s(), 0.25)
            os.environ["REWARD_API_SQLITE_TIMEOUT_S"] = "nan"
            _assert_equal("sqlite timeout non-finite fallback", _get_sqlite_timeout_s(), 30.0)
            os.environ["REWARD_API_SQLITE_TIMEOUT_S"] = "-1"
            _assert_equal("sqlite timeout negative fallback", _get_sqlite_timeout_s(), 30.0)
        finally:
            if original_sqlite_timeout is None:
                os.environ.pop("REWARD_API_SQLITE_TIMEOUT_S", None)
            else:
                os.environ["REWARD_API_SQLITE_TIMEOUT_S"] = original_sqlite_timeout
        lease_now = time.time()
        _assert_equal(
            "initial maintenance lease acquire",
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id="owner-a",
                lease_s=60.0,
                now_s=lease_now,
            ),
            True,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE maintenance_leases SET owner_id = ?, lease_until_s = ? WHERE name = ?;",
                    ("owner-a", "not-a-time", "cleanup"),
                )
        _assert_equal(
            "text-corrupt maintenance lease should be expired",
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id="owner-b",
                lease_s=60.0,
                now_s=lease_now + 1.0,
            ),
            True,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE maintenance_leases SET owner_id = ?, lease_until_s = ? WHERE name = ?;",
                    ("owner-b", 1.0e101, "cleanup"),
                )
        _assert_equal(
            "huge maintenance lease should be expired",
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id="owner-c",
                lease_s=60.0,
                now_s=lease_now + 2.0,
            ),
            True,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            row = conn.execute(
                "SELECT owner_id, lease_until_s FROM maintenance_leases WHERE name = ?;",
                ("cleanup",),
            ).fetchone()
        if row is None:
            raise AssertionError("maintenance lease disappeared")
        _assert_equal("maintenance lease repaired owner", row[0], "owner-c")
        if float(row[1]) <= lease_now + 2.0:
            raise AssertionError(f"maintenance lease was not overwritten with a future lease: {row!r}")
        _assert_equal(
            "valid maintenance lease still blocks another owner",
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id="owner-d",
                lease_s=60.0,
                now_s=lease_now + 3.0,
            ),
            False,
        )
        no_renew_now = lease_now + 10.0
        _assert_equal(
            "no-renew maintenance lease acquire",
            store.try_acquire_maintenance_lease(
                name="quarantine",
                owner_id="owner-a",
                lease_s=60.0,
                now_s=no_renew_now,
                allow_owner_renewal=False,
            ),
            True,
        )
        _assert_equal(
            "no-renew same owner blocked before expiry",
            store.try_acquire_maintenance_lease(
                name="quarantine",
                owner_id="owner-a",
                lease_s=60.0,
                now_s=no_renew_now + 1.0,
                allow_owner_renewal=False,
            ),
            False,
        )
        _assert_equal(
            "owner renewal default still works",
            store.try_acquire_maintenance_lease(
                name="owner-renewal",
                owner_id="owner-a",
                lease_s=60.0,
                now_s=no_renew_now,
            ),
            True,
        )
        _assert_equal(
            "same owner can renew by default",
            store.try_acquire_maintenance_lease(
                name="owner-renewal",
                owner_id="owner-a",
                lease_s=60.0,
                now_s=no_renew_now + 1.0,
            ),
            True,
        )
        _assert_equal(
            "no-renew same owner can reacquire after expiry",
            store.try_acquire_maintenance_lease(
                name="quarantine",
                owner_id="owner-a",
                lease_s=60.0,
                now_s=no_renew_now + 61.0,
                allow_owner_renewal=False,
            ),
            True,
        )
        _assert_equal(
            "no-renew different owner can take expired lease",
            store.try_acquire_maintenance_lease(
                name="quarantine",
                owner_id="owner-b",
                lease_s=60.0,
                now_s=no_renew_now + 122.0,
                allow_owner_renewal=False,
            ),
            True,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE maintenance_leases SET owner_id = ?, lease_until_s = ? WHERE name = ?;",
                    ("", lease_now + 120.0, "cleanup"),
                )
        _assert_equal(
            "blank maintenance lease owner should be expired",
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id="owner-e",
                lease_s=60.0,
                now_s=lease_now + 4.0,
            ),
            True,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE maintenance_leases SET owner_id = ?, lease_until_s = ? WHERE name = ?;",
                    (sqlite3.Binary(b"owner-e"), lease_now + 120.0, "cleanup"),
                )
        _assert_equal(
            "blob maintenance lease owner should be expired",
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id="owner-f",
                lease_s=60.0,
                now_s=lease_now + 5.0,
            ),
            True,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE maintenance_leases SET owner_id = ?, lease_until_s = ? WHERE name = ?;",
                    ("bad\x00owner", lease_now + 120.0, "cleanup"),
                )
        _assert_equal(
            "nul maintenance lease owner should be expired",
            store.try_acquire_maintenance_lease(
                name="cleanup",
                owner_id="owner-g",
                lease_s=60.0,
                now_s=lease_now + 6.0,
            ),
            True,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            index_names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index';"
                ).fetchall()
            }
            claim_plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT job_id FROM jobs
                WHERE status = ?
                ORDER BY created_at_s, job_id
                LIMIT 1;
                """,
                (JobStatus.QUEUED.value,),
            ).fetchall()
            fresh_cleanup_plan = conn.execute(
                f"""
                EXPLAIN QUERY PLAN
                SELECT job_id, kind, gt_file, proc_file, status, msg, created_at_s, finished_at_s, cleanup_next_retry_s
                FROM jobs
                WHERE status = ?
                  AND finished_at_s IS NOT NULL
                  AND {_CLEANUP_RETENTION_SQL} < ?
                  AND cleanup_next_retry_s IS NULL
                ORDER BY {_CLEANUP_RETENTION_SQL}, job_id
                LIMIT 10;
                """,
                (JobStatus.DONE.value, time.time() + 1.0),
            ).fetchall()
            retry_cleanup_plan = conn.execute(
                f"""
                EXPLAIN QUERY PLAN
                SELECT job_id, kind, gt_file, proc_file, status, msg, created_at_s, finished_at_s, cleanup_next_retry_s
                FROM jobs
                WHERE status = ?
                  AND finished_at_s IS NOT NULL
                  AND {_CLEANUP_RETENTION_SQL} < ?
                  AND cleanup_next_retry_s IS NOT NULL
                  AND cleanup_next_retry_s <= ?
                ORDER BY cleanup_next_retry_s, {_CLEANUP_RETENTION_SQL}, job_id
                LIMIT 10;
                """,
                (JobStatus.DONE.value, time.time() + 1.0, time.time() + 1.0),
            ).fetchall()
        _assert_equal("redundant finished index removed", "idx_jobs_finished" in index_names, False)
        _assert_equal("old claim index removed", "idx_jobs_status_created" in index_names, False)
        _assert_equal("claim order index created", "idx_jobs_status_created_job_id" in index_names, True)
        _assert_equal("old fresh cleanup index removed", "idx_jobs_status_finished_job_id" in index_names, False)
        _assert_equal("old retry cleanup index removed", "idx_jobs_status_cleanup_retry" in index_names, False)
        _assert_equal(
            "fresh cleanup index created",
            "idx_jobs_status_cleanup_retention_job_id" in index_names,
            True,
        )
        _assert_equal(
            "retry cleanup index created",
            "idx_jobs_status_cleanup_retry_retention_job_id" in index_names,
            True,
        )
        fresh_cleanup_plan_text = "\n".join(str(row) for row in fresh_cleanup_plan)
        retry_cleanup_plan_text = "\n".join(str(row) for row in retry_cleanup_plan)
        if "idx_jobs_status_cleanup_retention_job_id" not in fresh_cleanup_plan_text:
            raise AssertionError(f"fresh cleanup plan did not use retention index: {fresh_cleanup_plan!r}")
        if "idx_jobs_status_cleanup_retry_retention_job_id" not in retry_cleanup_plan_text:
            raise AssertionError(f"retry cleanup plan did not use retention index: {retry_cleanup_plan!r}")
        for label, plan in (
            ("claim query plan", claim_plan),
            ("fresh cleanup query plan", fresh_cleanup_plan),
            ("retry cleanup query plan", retry_cleanup_plan),
        ):
            if any("TEMP B-TREE" in str(row).upper() for row in plan):
                raise AssertionError(f"{label} still sorts via temp b-tree: {plan!r}")

        for job in (
            _job("fresh_done", root, status=JobStatus.DONE),
            _job("retry_done", root, status=JobStatus.DONE),
        ):
            if not store.enqueue(job, max_queue_size=10):
                raise AssertionError(f"enqueue failed for {job.job_id}")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
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
            [job.job_id for job in batch],
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
        with closing(sqlite3.connect(store.db_path)) as conn:
            before_updated = conn.execute(
                "SELECT updated_at_s FROM jobs WHERE job_id = ?;",
                ("fresh_done",),
            ).fetchone()[0]
        time.sleep(0.01)
        _assert_equal(
            "cleanup failure marking rowcount ignores missing rows",
            store.mark_cleanup_failed(
                job_ids=["fresh_done", "missing-cleanup-row"],
                retry_after_s=1.0,
                retry_max_s=1.0,
            ),
            1,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            after_updated = conn.execute(
                "SELECT updated_at_s FROM jobs WHERE job_id = ?;",
                ("fresh_done",),
            ).fetchone()[0]
        if after_updated <= before_updated:
            raise AssertionError("cleanup failure marking did not update updated_at_s")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET cleanup_next_retry_s = ? WHERE job_id = ?;",
                    (time.time() - 1.0, "retry_done"),
                )

        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET cleanup_next_retry_s = ? WHERE job_id = ?;",
                    (1.0e101, "fresh_done"),
                )
        _assert_equal("corrupt cleanup retry quarantine repair", store.quarantine_invalid_jobs(), 1)
        cleanup_retry_corrupt_batch = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=100,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "corrupt cleanup retry time should become cleanup-visible",
            "fresh_done" in [job.job_id for job in cleanup_retry_corrupt_batch],
            True,
        )
        fresh_done_snapshot = store.get_snapshot("fresh_done")
        if fresh_done_snapshot is None:
            raise AssertionError("fresh_done disappeared after cleanup retry normalization")
        _assert_equal("cleanup metadata corruption should not change status", fresh_done_snapshot.status, JobStatus.DONE)

        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET cleanup_attempts = ? WHERE job_id = ?;",
                    (1.0e100, "retry_done"),
                )
        _assert_equal(
            "corrupt cleanup attempts should be normalized before retry marking",
            store.mark_cleanup_failed(
                job_ids=["retry_done"],
                retry_after_s=1.0,
                retry_max_s=1.0,
            ),
            1,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            cleanup_attempts = conn.execute(
                "SELECT cleanup_attempts FROM jobs WHERE job_id = ?;",
                ("retry_done",),
            ).fetchone()[0]
        _assert_equal("cleanup attempts normalized then incremented", cleanup_attempts, 1)
        _assert_equal("max cleanup attempts is valid", _cleanup_attempts_value_is_valid(_MAX_SQLITE_INTEGER), True)
        _assert_equal(
            "overflow cleanup attempts is invalid",
            _cleanup_attempts_value_is_valid(_MAX_SQLITE_INTEGER + 1),
            False,
        )
        _assert_equal("negative cleanup attempts is invalid", _cleanup_attempts_value_is_valid(-1), False)
        _assert_equal("fractional cleanup attempts is invalid", _cleanup_attempts_value_is_valid(1.5), False)
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET cleanup_attempts = ? WHERE job_id = ?;",
                    (_MAX_SQLITE_INTEGER, "retry_done"),
                )
        _assert_equal(
            "max cleanup attempts retry marking",
            store.mark_cleanup_failed(
                job_ids=["retry_done"],
                retry_after_s=1.0,
                retry_max_s=1.0,
            ),
            1,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            max_cleanup_attempts = conn.execute(
                "SELECT cleanup_attempts FROM jobs WHERE job_id = ?;",
                ("retry_done",),
            ).fetchone()[0]
        _assert_equal(
            "cleanup attempts saturates at sqlite integer max",
            max_cleanup_attempts,
            _MAX_SQLITE_INTEGER,
        )
        sqlite_int_min = -9_223_372_036_854_775_808
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET cleanup_attempts = ? WHERE job_id = ?;",
                    (sqlite_int_min, "retry_done"),
                )
        _assert_equal("sqlite int min cleanup attempts quarantine repair", store.quarantine_invalid_jobs(), 1)
        _assert_equal(
            "sqlite int min cleanup attempts repair does not break stats",
            store.stats(),
            {"queued": 1, "running": 0, "jobs": 3},
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            repaired_cleanup_attempts = conn.execute(
                "SELECT cleanup_attempts FROM jobs WHERE job_id = ?;",
                ("retry_done",),
            ).fetchone()[0]
        _assert_equal("sqlite int min cleanup attempts normalized", repaired_cleanup_attempts, 0)

        running_requeue = _job("running_requeue", root, status=JobStatus.RUNNING)
        if not store.enqueue(running_requeue, max_queue_size=10):
            raise AssertionError("enqueue failed for running requeue job")
        _assert_equal(
            "claimed requeue guarded update",
            store.requeue_claimed(job_id="running_requeue", worker_id=""),
            False,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                    ("worker-1", "running_requeue"),
                )
        _assert_equal(
            "claimed requeue success",
            store.requeue_claimed(job_id="running_requeue", worker_id="worker-1"),
            True,
        )
        snapshot = store.get_snapshot("running_requeue")
        if snapshot is None:
            raise AssertionError("requeued job disappeared")
        _assert_equal("requeued status", snapshot.status, JobStatus.QUEUED)

        finish_guard = _job("finish_guard", root, status=JobStatus.RUNNING)
        if not store.enqueue(finish_guard, max_queue_size=10):
            raise AssertionError("enqueue failed for finish_guard")
        _set_worker_id(store, "finish_guard", "worker-a")
        _assert_equal(
            "finish rejects wrong worker",
            store.finish(
                job_id="finish_guard",
                status=JobStatus.DONE,
                reward=1.0,
                msg="wrong worker",
                worker_id="worker-b",
            ),
            False,
        )
        finish_guard_snapshot = store.get_snapshot("finish_guard")
        if finish_guard_snapshot is None:
            raise AssertionError("finish guard job disappeared")
        _assert_equal("wrong-worker finish kept running", finish_guard_snapshot.status, JobStatus.RUNNING)
        _assert_equal(
            "finish accepts owning worker",
            store.finish(
                job_id="finish_guard",
                status=JobStatus.DONE,
                reward=1.0,
                msg="owning worker",
                worker_id="worker-a",
            ),
            True,
        )
        finish_guard_done = store.get_snapshot("finish_guard")
        if finish_guard_done is None:
            raise AssertionError("finished guard job disappeared")
        _assert_equal("owning-worker finish status", finish_guard_done.status, JobStatus.DONE)

        non_finite_finish = _job("non_finite_finish", root, status=JobStatus.RUNNING)
        if not store.enqueue(non_finite_finish, max_queue_size=10):
            raise AssertionError("enqueue failed for non_finite_finish")
        _assert_equal(
            "finish rejects non-finite reward",
            store.finish(
                job_id="non_finite_finish",
                status=JobStatus.DONE,
                reward=float("nan"),
                msg="done",
            ),
            True,
        )
        non_finite_snapshot = store.get_snapshot("non_finite_finish")
        if non_finite_snapshot is None:
            raise AssertionError("non-finite finish job disappeared")
        _assert_equal("non-finite finish status", non_finite_snapshot.status, JobStatus.ERROR)
        _assert_equal("non-finite finish reward", non_finite_snapshot.reward, 0.0)
        _assert_equal("non-finite finish msg", non_finite_snapshot.msg, "done; invalid reward value")

        huge_finish = _job("huge_finish", root, status=JobStatus.RUNNING)
        if not store.enqueue(huge_finish, max_queue_size=10):
            raise AssertionError("enqueue failed for huge_finish")
        _assert_equal(
            "finish rejects huge reward",
            store.finish(
                job_id="huge_finish",
                status=JobStatus.DONE,
                reward=1.0e101,
                msg="",
            ),
            True,
        )
        huge_snapshot = store.get_snapshot("huge_finish")
        if huge_snapshot is None:
            raise AssertionError("huge finish job disappeared")
        _assert_equal("huge finish status", huge_snapshot.status, JobStatus.ERROR)
        _assert_equal("huge finish reward", huge_snapshot.reward, 0.0)
        _assert_equal("huge finish msg", huge_snapshot.msg, "invalid reward value")

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_trigger_migration_"))
        trigger_root = Path(tmp)
        trigger_db = trigger_root / "jobs.sqlite3"
        with closing(sqlite3.connect(trigger_db)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        thread_dir TEXT NOT NULL,
                        gt_file TEXT NOT NULL,
                        proc_file TEXT NOT NULL,
                        answer_position TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'reward',
                        status TEXT NOT NULL,
                        created_at_s REAL NOT NULL,
                        started_at_s REAL,
                        finished_at_s REAL,
                        reward REAL,
                        msg TEXT NOT NULL DEFAULT '',
                        worker_id TEXT,
                        updated_at_s REAL NOT NULL,
                        cleanup_next_retry_s REAL,
                        cleanup_attempts INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
                conn.execute("CREATE TABLE job_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);")
                conn.executemany(
                    "INSERT INTO job_counters(name, value) VALUES (?, 0);",
                    [("queued",), ("running",), ("total",)],
                )
                conn.execute(
                    """
                    CREATE TRIGGER trg_jobs_counter_insert_queued
                    AFTER INSERT ON jobs
                    WHEN NEW.status = 'queued'
                    BEGIN
                        UPDATE job_counters SET value = value + 99 WHERE name = 'queued';
                    END;
                    """
                )
        trigger_store = SqliteJobStore(trigger_db)
        cleanup.callback(trigger_store.close)
        trigger_store.init()
        if not trigger_store.enqueue(_job("trigger_fixed", trigger_root), max_queue_size=10):
            raise AssertionError("enqueue failed after trigger recreation")
        _assert_equal(
            "recreated queued trigger increments by one",
            trigger_store.stats(),
            {"queued": 1, "running": 0, "jobs": 1},
        )
        trigger_claim = trigger_store.claim_next(worker_id="trigger-worker", max_running_jobs=1)
        if trigger_claim is None:
            raise AssertionError("claim failed after trigger recreation")
        _assert_equal(
            "recreated update trigger counters",
            trigger_store.stats(),
            {"queued": 0, "running": 1, "jobs": 1},
        )
        _assert_equal(
            "recreated trigger finish guard",
            trigger_store.finish(
                job_id=trigger_claim.job_id,
                status=JobStatus.DONE,
                reward=1.0,
                msg="done",
                worker_id="trigger-worker",
            ),
            True,
        )
        _assert_equal(
            "recreated finish counters",
            trigger_store.stats(),
            {"queued": 0, "running": 0, "jobs": 1},
        )
        _assert_equal("recreated delete trigger", trigger_store.delete_jobs(job_ids=[trigger_claim.job_id]), 1)
        _assert_equal(
            "recreated delete counters",
            trigger_store.stats(),
            {"queued": 0, "running": 0, "jobs": 0},
        )

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_cleanup_quarantine_count_"))
        root = Path(tmp)
        store = _CountingQuarantineStore(root / "jobs.sqlite3")
        cleanup.callback(store.close)
        store.init()
        old_finished_at = time.time() - 7200.0
        for job_id in ("count_retry", "count_fresh_1", "count_fresh_2"):
            job = _job(job_id, root, status=JobStatus.DONE)
            job.created_at_s = old_finished_at
            job.finished_at_s = old_finished_at
            if not store.enqueue(job, max_queue_size=10):
                raise AssertionError(f"enqueue failed for {job_id}")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET cleanup_next_retry_s = ? WHERE job_id = ?;",
                    (time.time() - 1.0, "count_retry"),
                )
        store.quarantine_calls = 0
        cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=3,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "cleanup batch avoids hot-path quarantine",
            store.quarantine_calls,
            0,
        )
        _assert_equal("explicit quarantine entry point", store.quarantine_invalid_jobs(), 0)
        _assert_equal("explicit quarantine counted once", store.quarantine_calls, 1)
        _assert_equal(
            "cleanup batch still returns retry and fresh jobs",
            sorted(job.job_id for job in cleanup_jobs),
            ["count_fresh_1", "count_fresh_2", "count_retry"],
        )

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_claim_order_"))
        root = Path(tmp)
        store = SqliteJobStore(root / "jobs.sqlite3")
        cleanup.callback(store.close)
        store.init()
        for job_id in ("claim-z", "claim-a"):
            if not store.enqueue(_job(job_id, root), max_queue_size=10):
                raise AssertionError(f"enqueue failed for {job_id}")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE jobs SET created_at_s = ?;", (1234.0,))
        claimed = store.claim_next(worker_id="worker", max_running_jobs=None)
        if claimed is None:
            raise AssertionError("deterministic claim returned no job")
        _assert_equal("deterministic returning claim tie-break", claimed.job_id, "claim-a")

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_claim_order_legacy_"))
        root = Path(tmp)
        store = SqliteJobStore(root / "jobs.sqlite3")
        cleanup.callback(store.close)
        store.init()
        for job_id in ("legacy-z", "legacy-a"):
            if not store.enqueue(_job(job_id, root), max_queue_size=10):
                raise AssertionError(f"enqueue failed for {job_id}")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE jobs SET created_at_s = ?;", (1234.0,))

        def fail_returning(conn: sqlite3.Connection, *, now: float, worker_id: str):
            raise sqlite3.OperationalError("RETURNING unsupported")

        store._claim_next_returning = fail_returning  # type: ignore[method-assign]
        claimed = store.claim_next(worker_id="worker", max_running_jobs=None)
        if claimed is None:
            raise AssertionError("legacy deterministic claim returned no job")
        _assert_equal("deterministic legacy claim tie-break", claimed.job_id, "legacy-a")

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_null_job_id_queue_head_"))
        root = Path(tmp)
        _test_null_job_id_queue_head_claim_repair(root, force_legacy=False, cleanup=cleanup)
        _test_null_job_id_queue_head_claim_repair(root, force_legacy=True, cleanup=cleanup)

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_job_store_legacy_"))
        root = Path(tmp)
        db_path = root / "legacy.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        thread_dir TEXT NOT NULL,
                        gt_file TEXT NOT NULL,
                        proc_file TEXT NOT NULL,
                        answer_position TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at_s REAL NOT NULL,
                        started_at_s REAL,
                        finished_at_s REAL,
                        reward REAL,
                        msg TEXT NOT NULL DEFAULT '',
                        worker_id TEXT,
                        updated_at_s REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id,
                        thread_dir,
                        gt_file,
                        proc_file,
                        answer_position,
                        status,
                        created_at_s,
                        started_at_s,
                        finished_at_s,
                        reward,
                        msg,
                        worker_id,
                        updated_at_s
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "legacy_reward",
                        "thread_1",
                        str(root / "target.xlsx"),
                        str(root / "output.xlsx"),
                        "Sheet1!A1",
                        JobStatus.QUEUED.value,
                        time.time(),
                        None,
                        None,
                        None,
                        "",
                        None,
                        time.time(),
                    ),
                )
        legacy_store = SqliteJobStore(db_path)
        cleanup.callback(legacy_store.close)
        legacy_store.init()
        legacy_snapshot = legacy_store.get_snapshot("legacy_reward")
        if legacy_snapshot is None:
            raise AssertionError("legacy job disappeared after migration")
        _assert_equal("legacy job kind migration", legacy_snapshot.kind, JobKind.REWARD)

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(temporary_directory(prefix="async_reward_api_job_store_bad_enum_"))
        root = Path(tmp)
        store = SqliteJobStore(root / "bad_enum.sqlite3")
        cleanup.callback(store.close)
        store.init()
        bad_kind = _job("bad_kind", root)
        if not store.enqueue(bad_kind, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_kind")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE jobs SET kind = ? WHERE job_id = ?;", ("bogus", "bad_kind"))
        preclaim_bad_kind_snapshot = store.get_snapshot("bad_kind")
        if preclaim_bad_kind_snapshot is None:
            raise AssertionError("bad kind preclaim job disappeared")
        _assert_equal("invalid kind preclaim snapshot status", preclaim_bad_kind_snapshot.status, JobStatus.ERROR)
        claimed = store.claim_next(worker_id="worker", max_running_jobs=None)
        _assert_equal("invalid kind claim should be quarantined", claimed, None)
        bad_kind_snapshot = store.get_snapshot("bad_kind")
        if bad_kind_snapshot is None:
            raise AssertionError("bad kind job disappeared")
        _assert_equal("invalid kind status", bad_kind_snapshot.status, JobStatus.ERROR)
        if "invalid persisted job row" not in bad_kind_snapshot.msg:
            raise AssertionError(f"bad kind diagnostic missing: {bad_kind_snapshot.msg!r}")
        with closing(sqlite3.connect(store.db_path)) as conn:
            row = conn.execute(
                "SELECT kind, finished_at_s FROM jobs WHERE job_id = ?;",
                ("bad_kind",),
            ).fetchone()
        if row is None:
            raise AssertionError("bad kind row disappeared")
        _assert_equal("invalid kind normalized", row[0], JobKind.REWARD.value)
        bad_kind_finished_at = row[1]
        if bad_kind_finished_at is None:
            raise AssertionError("bad kind row was not made terminal")
        second_bad_kind_snapshot = store.get_snapshot("bad_kind")
        if second_bad_kind_snapshot is None:
            raise AssertionError("bad kind second snapshot disappeared")
        with closing(sqlite3.connect(store.db_path)) as conn:
            second_finished_at = conn.execute(
                "SELECT finished_at_s FROM jobs WHERE job_id = ?;",
                ("bad_kind",),
            ).fetchone()[0]
        _assert_equal("invalid kind quarantine idempotent", second_finished_at, bad_kind_finished_at)

        bad_status = _job("bad_status", root)
        if not store.enqueue(bad_status, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_status")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?;", ("bogus", "bad_status"))
        bad_status_snapshot = store.get_snapshot("bad_status")
        if bad_status_snapshot is None:
            raise AssertionError("bad status job disappeared")
        _assert_equal("invalid status snapshot coerces to error", bad_status_snapshot.status, JobStatus.ERROR)
        if "invalid persisted job row" not in bad_status_snapshot.msg:
            raise AssertionError(f"bad status diagnostic missing: {bad_status_snapshot.msg!r}")
        _assert_equal("invalid status quarantine", store.quarantine_invalid_jobs(), 1)
        with closing(sqlite3.connect(store.db_path)) as conn:
            row = conn.execute(
                "SELECT status, finished_at_s FROM jobs WHERE job_id = ?;",
                ("bad_status",),
            ).fetchone()
        if row is None:
            raise AssertionError("bad status row disappeared")
        _assert_equal("invalid status persisted as error", row[0], JobStatus.ERROR.value)
        if row[1] is None:
            raise AssertionError("invalid status row was not made terminal")
        cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=10,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "invalid status cleanup candidate",
            "bad_status" in [job.job_id for job in cleanup_jobs],
            True,
        )
        _assert_equal("invalid status cleanup delete", store.delete_jobs(job_ids=["bad_status"]), 1)

        bad_numeric = _job("bad_numeric", root, status=JobStatus.DONE)
        if not store.enqueue(bad_numeric, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_numeric")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET reward = ?, created_at_s = ? WHERE job_id = ?;",
                    (1.0e101, "C:\\secret\\jobs.sqlite3", "bad_numeric"),
                )
        _assert_equal("bad numeric quarantine", store.quarantine_invalid_jobs(), 1)
        bad_numeric_snapshot = store.get_snapshot("bad_numeric")
        if bad_numeric_snapshot is None:
            raise AssertionError("bad numeric job disappeared")
        _assert_equal("invalid numeric snapshot coerces to error", bad_numeric_snapshot.status, JobStatus.ERROR)
        _assert_equal("invalid numeric reward coerced", bad_numeric_snapshot.reward, 0.0)
        if bad_numeric_snapshot.created_at_s <= 0.0:
            raise AssertionError(f"invalid numeric created_at was not normalized: {bad_numeric_snapshot}")
        if "invalid persisted job row" not in bad_numeric_snapshot.msg:
            raise AssertionError(f"bad numeric diagnostic missing: {bad_numeric_snapshot.msg!r}")
        if "C:\\secret" in bad_numeric_snapshot.msg:
            raise AssertionError(f"bad numeric diagnostic leaked raw value: {bad_numeric_snapshot.msg!r}")

        bad_claim_numeric = _job("bad_claim_numeric", root)
        if not store.enqueue(bad_claim_numeric, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_claim_numeric")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET created_at_s = ? WHERE job_id = ?;",
                    ("C:\\secret\\claim.sqlite3", "bad_claim_numeric"),
                )
        _assert_equal(
            "invalid claim row should not be returned",
            store.claim_next(worker_id="worker", max_running_jobs=None),
            None,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            persisted_msg = conn.execute(
                "SELECT msg FROM jobs WHERE job_id = ?;",
                ("bad_claim_numeric",),
            ).fetchone()[0]
        if "C:\\secret" in persisted_msg:
            raise AssertionError(f"claim invalid-row message leaked raw value: {persisted_msg!r}")
        bad_claim_snapshot = store.get_snapshot("bad_claim_numeric")
        if bad_claim_snapshot is None:
            raise AssertionError("bad claim numeric job disappeared")
        if "C:\\secret" in bad_claim_snapshot.msg:
            raise AssertionError(f"bad claim snapshot leaked raw value: {bad_claim_snapshot.msg!r}")

        running_null_started = _job("running_null_started", root, status=JobStatus.RUNNING)
        if not store.enqueue(running_null_started, max_queue_size=10):
            raise AssertionError("enqueue failed for running_null_started")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET started_at_s = ? WHERE job_id = ?;",
                    (None, "running_null_started"),
                )
        running_null_snapshot = store.get_snapshot("running_null_started")
        if running_null_snapshot is None:
            raise AssertionError("running null-start row disappeared")
        _assert_equal("running null-start snapshot status", running_null_snapshot.status, JobStatus.ERROR)

        bad_empty_answer_position = _job("bad_empty_answer_position", root)
        if not store.enqueue(bad_empty_answer_position, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_empty_answer_position")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET answer_position = ? WHERE job_id = ?;",
                    ("   ", "bad_empty_answer_position"),
                )
        _assert_equal(
            "empty reward answer_position should not be claimed",
            store.claim_next(worker_id="worker", max_running_jobs=None),
            None,
        )
        bad_empty_answer_snapshot = store.get_snapshot("bad_empty_answer_position")
        if bad_empty_answer_snapshot is None:
            raise AssertionError("empty answer_position row disappeared")
        _assert_equal(
            "empty reward answer_position snapshot status",
            bad_empty_answer_snapshot.status,
            JobStatus.ERROR,
        )

        bad_empty_job_id = _job("bad_empty_job_id", root)
        if not store.enqueue(bad_empty_job_id, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_empty_job_id")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                empty_job_rowid = conn.execute(
                    "SELECT rowid FROM jobs WHERE job_id = ?;",
                    ("bad_empty_job_id",),
                ).fetchone()[0]
                conn.execute("UPDATE jobs SET job_id = ? WHERE job_id = ?;", ("", "bad_empty_job_id"))
        _assert_equal(
            "empty job_id should not be claimed",
            store.claim_next(worker_id="worker", max_running_jobs=None),
            None,
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            empty_job_snapshot_row = conn.execute(
                "SELECT status, msg FROM jobs WHERE job_id = ?;",
                (f"invalid-{empty_job_rowid}",),
            ).fetchone()
        if empty_job_snapshot_row is None:
            raise AssertionError("empty job_id row was not repaired")
        _assert_equal("empty job_id status", empty_job_snapshot_row[0], JobStatus.ERROR.value)
        if "job_id=invalid" not in str(empty_job_snapshot_row[1]):
            raise AssertionError(f"empty job_id diagnostic missing: {empty_job_snapshot_row[1]!r}")

        bad_blob_job_id = _job("bad_blob_job_id", root)
        if not store.enqueue(bad_blob_job_id, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_blob_job_id")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                blob_job_rowid = conn.execute(
                    "SELECT rowid FROM jobs WHERE job_id = ?;",
                    ("bad_blob_job_id",),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE jobs SET job_id = ? WHERE job_id = ?;",
                    (sqlite3.Binary(b"abc"), "bad_blob_job_id"),
                )
        _assert_equal(
            "blob job_id should not break stats",
            store.stats()["jobs"] >= 1,
            True,
        )
        store.quarantine_invalid_jobs()
        with closing(sqlite3.connect(store.db_path)) as conn:
            blob_job_snapshot_row = conn.execute(
                "SELECT status, msg FROM jobs WHERE job_id = ?;",
                (f"invalid-{blob_job_rowid}",),
            ).fetchone()
        if blob_job_snapshot_row is None:
            raise AssertionError("blob job_id row was not repaired")
        _assert_equal("blob job_id status", blob_job_snapshot_row[0], JobStatus.ERROR.value)
        if "job_id=invalid" not in str(blob_job_snapshot_row[1]):
            raise AssertionError(f"blob job_id diagnostic missing: {blob_job_snapshot_row[1]!r}")

        bad_null_job_id = _job("bad_null_job_id", root)
        if not store.enqueue(bad_null_job_id, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_null_job_id")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                null_job_rowid = conn.execute(
                    "SELECT rowid FROM jobs WHERE job_id = ?;",
                    ("bad_null_job_id",),
                ).fetchone()[0]
                conn.execute("UPDATE jobs SET job_id = NULL WHERE job_id = ?;", ("bad_null_job_id",))
        _assert_equal(
            "null job_id should not be claimed",
            store.claim_next(worker_id="worker", max_running_jobs=None),
            None,
        )
        store.quarantine_invalid_jobs()
        with closing(sqlite3.connect(store.db_path)) as conn:
            null_job_id_rows = conn.execute(
                "SELECT job_id, status, msg FROM jobs WHERE job_id = ?;",
                (f"invalid-{null_job_rowid}",),
            ).fetchall()
        _assert_equal("null job_id row repaired once", len(null_job_id_rows), 1)
        _assert_equal("null job_id status", null_job_id_rows[0][1], JobStatus.ERROR.value)
        if "job_id=invalid" not in str(null_job_id_rows[0][2]):
            raise AssertionError(f"null job_id diagnostic missing: {null_job_id_rows[0][2]!r}")

        bad_null_job_id_collision = _job("bad_null_job_id_collision", root)
        if not store.enqueue(bad_null_job_id_collision, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_null_job_id_collision")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE jobs SET job_id = NULL WHERE job_id = ?;", ("bad_null_job_id_collision",))
                rowid = conn.execute("SELECT rowid FROM jobs WHERE job_id IS NULL;").fetchone()[0]
                collision_job = _job(f"invalid-{rowid}", root, status=JobStatus.DONE)
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id,
                        thread_dir,
                        gt_file,
                        proc_file,
                        answer_position,
                        kind,
                        status,
                        created_at_s,
                        started_at_s,
                        finished_at_s,
                        reward,
                        msg,
                        updated_at_s
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        collision_job.job_id,
                        collision_job.thread_dir,
                        str(collision_job.gt_file),
                        str(collision_job.proc_file),
                        collision_job.answer_position,
                        collision_job.kind.value,
                        collision_job.status.value,
                        collision_job.created_at_s,
                        collision_job.started_at_s,
                        collision_job.finished_at_s,
                        collision_job.reward,
                        collision_job.msg,
                        collision_job.created_at_s,
                    ),
                )
        _assert_equal(
            "null job_id collision should not block stats",
            store.stats()["jobs"] >= 1,
            True,
        )
        store.quarantine_invalid_jobs()
        with closing(sqlite3.connect(store.db_path)) as conn:
            repaired_collision_rows = conn.execute(
                "SELECT job_id, status FROM jobs WHERE job_id LIKE ?;",
                (f"invalid-{rowid}-%",),
            ).fetchall()
        _assert_equal("null job_id collision repaired with suffix", len(repaired_collision_rows), 1)
        _assert_equal("null job_id collision status", repaired_collision_rows[0][1], JobStatus.ERROR.value)

        with ExitStack() as nested_cleanup:
            tmp = nested_cleanup.enter_context(temporary_directory(prefix="async_reward_api_scoped_snapshot_repair_"))
            scoped_root = Path(tmp)
            scoped_store = _CountingRepairStore(scoped_root / "jobs.sqlite3")
            nested_cleanup.callback(scoped_store.close)
            scoped_store.init()
            scoped_store.missing_job_id_repairs = 0
            _assert_equal(
                "scoped valid enqueue",
                scoped_store.enqueue(_job("valid-scoped-job", scoped_root), max_queue_size=10),
                True,
            )
            _assert_equal(
                "scoped null-id enqueue",
                scoped_store.enqueue(_job("null-scoped-job", scoped_root), max_queue_size=10),
                True,
            )
            with closing(sqlite3.connect(scoped_store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET job_id = NULL WHERE job_id = ?;",
                        ("null-scoped-job",),
                    )
            scoped_snapshot = scoped_store.get_snapshot("valid-scoped-job")
            if scoped_snapshot is None:
                raise AssertionError("valid scoped snapshot disappeared")
            _assert_equal("scoped snapshot should not run global repair", scoped_store.missing_job_id_repairs, 0)
            with closing(sqlite3.connect(scoped_store.db_path)) as conn:
                null_rows = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_id IS NULL;").fetchone()[0]
            _assert_equal("scoped snapshot left unrelated null-id row alone", null_rows, 1)
            scoped_store.quarantine_invalid_jobs()
            _assert_equal("global quarantine repair count", scoped_store.missing_job_id_repairs, 1)
            _assert_equal("stats after null-id repair", scoped_store.stats()["jobs"], 2)

        with ExitStack() as nested_cleanup:
            tmp = nested_cleanup.enter_context(temporary_directory(prefix="async_reward_api_running_worker_id_"))
            worker_root = Path(tmp)
            worker_store = SqliteJobStore(worker_root / "jobs.sqlite3")
            nested_cleanup.callback(worker_store.close)
            worker_store.init()
            if not worker_store.enqueue(_job("bad-running-worker", worker_root, status=JobStatus.RUNNING), max_queue_size=10):
                raise AssertionError("enqueue failed for bad-running-worker")
            if not worker_store.enqueue(_job("queued-after-worker-corruption", worker_root), max_queue_size=10):
                raise AssertionError("enqueue failed for queued-after-worker-corruption")
            with closing(sqlite3.connect(worker_store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        ("", "bad-running-worker"),
                    )
            worker_store.quarantine_invalid_jobs()
            claimed_after_blank_worker = worker_store.claim_next(worker_id="worker", max_running_jobs=1)
            if claimed_after_blank_worker is None:
                raise AssertionError("blank worker_id running row still blocked claim_next")
            _assert_equal(
                "claim after blank worker_id running row",
                claimed_after_blank_worker.job_id,
                "queued-after-worker-corruption",
            )
            bad_worker_snapshot = worker_store.get_snapshot("bad-running-worker")
            if bad_worker_snapshot is None:
                raise AssertionError("blank worker_id row disappeared")
            _assert_equal("blank worker_id status", bad_worker_snapshot.status, JobStatus.ERROR)
        if "worker_id=invalid" not in bad_worker_snapshot.msg:
            raise AssertionError(f"blank worker_id diagnostic missing: {bad_worker_snapshot.msg!r}")

        with ExitStack() as nested_cleanup:
            tmp = nested_cleanup.enter_context(temporary_directory(prefix="async_reward_api_running_worker_id_blob_"))
            worker_root = Path(tmp)
            worker_store = SqliteJobStore(worker_root / "jobs.sqlite3")
            nested_cleanup.callback(worker_store.close)
            worker_store.init()
            if not worker_store.enqueue(_job("bad-blob-worker", worker_root, status=JobStatus.RUNNING), max_queue_size=10):
                raise AssertionError("enqueue failed for bad-blob-worker")
            if not worker_store.enqueue(_job("queued-after-blob-worker", worker_root), max_queue_size=10):
                raise AssertionError("enqueue failed for queued-after-blob-worker")
            with closing(sqlite3.connect(worker_store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        (sqlite3.Binary(b"worker"), "bad-blob-worker"),
                    )
            worker_store.quarantine_invalid_jobs()
            claimed_after_blob_worker = worker_store.claim_next(worker_id="worker", max_running_jobs=1)
            if claimed_after_blob_worker is None:
                raise AssertionError("blob worker_id running row still blocked claim_next")
            _assert_equal("claim after blob worker_id", claimed_after_blob_worker.job_id, "queued-after-blob-worker")
            bad_blob_worker_snapshot = worker_store.get_snapshot("bad-blob-worker")
            if bad_blob_worker_snapshot is None:
                raise AssertionError("blob worker_id row disappeared")
            _assert_equal("blob worker_id status", bad_blob_worker_snapshot.status, JobStatus.ERROR)
            if "worker_id=invalid" not in bad_blob_worker_snapshot.msg:
                raise AssertionError(f"blob worker_id diagnostic missing: {bad_blob_worker_snapshot.msg!r}")

        with ExitStack() as nested_cleanup:
            tmp = nested_cleanup.enter_context(temporary_directory(prefix="async_reward_api_running_worker_id_nul_"))
            worker_root = Path(tmp)
            worker_store = SqliteJobStore(worker_root / "jobs.sqlite3")
            nested_cleanup.callback(worker_store.close)
            worker_store.init()
            if not worker_store.enqueue(_job("bad-nul-worker", worker_root, status=JobStatus.RUNNING), max_queue_size=10):
                raise AssertionError("enqueue failed for bad-nul-worker")
            if not worker_store.enqueue(_job("queued-after-nul-worker", worker_root), max_queue_size=10):
                raise AssertionError("enqueue failed for queued-after-nul-worker")
            with closing(sqlite3.connect(worker_store.db_path)) as conn:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET worker_id = ? WHERE job_id = ?;",
                        ("bad\x00worker", "bad-nul-worker"),
                    )
            worker_store.quarantine_invalid_jobs()
            claimed_after_nul_worker = worker_store.claim_next(worker_id="worker", max_running_jobs=1)
            if claimed_after_nul_worker is None:
                raise AssertionError("nul worker_id running row still blocked claim_next")
            _assert_equal("claim after nul worker_id", claimed_after_nul_worker.job_id, "queued-after-nul-worker")
            bad_nul_worker_snapshot = worker_store.get_snapshot("bad-nul-worker")
            if bad_nul_worker_snapshot is None:
                raise AssertionError("nul worker_id row disappeared")
            _assert_equal("nul worker_id status", bad_nul_worker_snapshot.status, JobStatus.ERROR)
            if "worker_id=invalid" not in bad_nul_worker_snapshot.msg:
                raise AssertionError(f"nul worker_id diagnostic missing: {bad_nul_worker_snapshot.msg!r}")

        quarantine_idempotent = _job("quarantine_idempotent", root)
        if not store.enqueue(quarantine_idempotent, max_queue_size=10):
            raise AssertionError("enqueue failed for quarantine_idempotent")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET answer_position = ? WHERE job_id = ?;",
                    ("   ", "quarantine_idempotent"),
                )
        store.quarantine_invalid_jobs()
        with closing(sqlite3.connect(store.db_path)) as conn:
            first_updated_at = conn.execute(
                "SELECT updated_at_s FROM jobs WHERE job_id = ?;",
                ("quarantine_idempotent",),
            ).fetchone()[0]
        time.sleep(0.01)
        store.quarantine_invalid_jobs()
        with closing(sqlite3.connect(store.db_path)) as conn:
            second_updated_at = conn.execute(
                "SELECT updated_at_s FROM jobs WHERE job_id = ?;",
                ("quarantine_idempotent",),
            ).fetchone()[0]
        _assert_equal("already quarantined invalid row was rewritten", second_updated_at, first_updated_at)

        with ExitStack() as nested_cleanup:
            tmp = nested_cleanup.enter_context(temporary_directory(prefix="async_reward_api_nullable_legacy_enums_"))
            legacy_root = Path(tmp)
            legacy_db = legacy_root / "jobs.sqlite3"
            with closing(sqlite3.connect(legacy_db)) as conn:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE jobs (
                            job_id TEXT PRIMARY KEY,
                            thread_dir TEXT,
                            gt_file TEXT,
                            proc_file TEXT,
                            answer_position TEXT,
                            kind TEXT,
                            status TEXT,
                            created_at_s REAL,
                            started_at_s REAL,
                            finished_at_s REAL,
                            reward REAL,
                            msg TEXT,
                            worker_id TEXT,
                            updated_at_s REAL
                        );
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            job_id,
                            thread_dir,
                            gt_file,
                            proc_file,
                            answer_position,
                            kind,
                            status,
                            created_at_s,
                            updated_at_s
                        )
                        VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?);
                        """,
                        (
                            "legacy-null-enums",
                            "thread_1",
                            str(legacy_root / "target.xlsx"),
                            str(legacy_root / "output.xlsx"),
                            "Sheet1!A1",
                            time.time(),
                            time.time(),
                        ),
                    )
            legacy_store = SqliteJobStore(legacy_db)
            nested_cleanup.callback(legacy_store.close)
            legacy_store.init()
            with closing(sqlite3.connect(legacy_store.db_path)) as conn:
                legacy_row = conn.execute(
                    "SELECT kind, status, finished_at_s, msg FROM jobs WHERE job_id = ?;",
                    ("legacy-null-enums",),
                ).fetchone()
            if legacy_row is None:
                raise AssertionError("legacy null enum row disappeared")
            _assert_equal("legacy null kind repaired", legacy_row[0], JobKind.REWARD.value)
            _assert_equal("legacy null status quarantined", legacy_row[1], JobStatus.ERROR.value)
            if legacy_row[2] is None:
                raise AssertionError("legacy null status row did not get terminal timestamp")
            if "kind=invalid" not in str(legacy_row[3]) or "status=invalid" not in str(legacy_row[3]):
                raise AssertionError(f"legacy null enum diagnostic missing: {legacy_row[3]!r}")

        with ExitStack() as nested_cleanup:
            tmp = nested_cleanup.enter_context(temporary_directory(prefix="async_reward_api_legacy_no_worker_id_"))
            legacy_root = Path(tmp)
            legacy_db = legacy_root / "jobs.sqlite3"
            with closing(sqlite3.connect(legacy_db)) as conn:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE jobs (
                            job_id TEXT PRIMARY KEY,
                            thread_dir TEXT,
                            gt_file TEXT,
                            proc_file TEXT,
                            answer_position TEXT,
                            kind TEXT,
                            status TEXT,
                            created_at_s REAL,
                            started_at_s REAL,
                            finished_at_s REAL,
                            reward REAL,
                            msg TEXT,
                            updated_at_s REAL
                        );
                        """
                    )
                    started_at_s = time.time()
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            job_id,
                            thread_dir,
                            gt_file,
                            proc_file,
                            answer_position,
                            kind,
                            status,
                            created_at_s,
                            started_at_s,
                            updated_at_s
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            "legacy-no-worker-id",
                            "thread_1",
                            str(legacy_root / "target.xlsx"),
                            str(legacy_root / "output.xlsx"),
                            "Sheet1!A1",
                            JobKind.REWARD.value,
                            JobStatus.RUNNING.value,
                            started_at_s,
                            started_at_s,
                            started_at_s,
                        ),
                    )
            legacy_store = SqliteJobStore(legacy_db)
            nested_cleanup.callback(legacy_store.close)
            legacy_store.init()
            with closing(sqlite3.connect(legacy_store.db_path)) as conn:
                legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs);").fetchall()}
                legacy_row = conn.execute(
                    "SELECT status, msg FROM jobs WHERE job_id = ?;",
                    ("legacy-no-worker-id",),
                ).fetchone()
            _assert_equal("legacy worker_id column added", "worker_id" in legacy_columns, True)
            if legacy_row is None:
                raise AssertionError("legacy no-worker_id row disappeared")
            _assert_equal("legacy no-worker_id running row quarantined", legacy_row[0], JobStatus.ERROR.value)
            if "worker_id=invalid" not in str(legacy_row[1]):
                raise AssertionError(f"legacy no-worker_id diagnostic missing: {legacy_row[1]!r}")

        queued_finished = _job("queued_finished", root)
        if not store.enqueue(queued_finished, max_queue_size=10):
            raise AssertionError("enqueue failed for queued_finished")
        old_finished_at = time.time() - 7200.0
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET finished_at_s = ? WHERE job_id = ?;",
                    (old_finished_at, "queued_finished"),
                )
        queued_finished_snapshot = store.get_snapshot("queued_finished")
        if queued_finished_snapshot is None:
            raise AssertionError("queued finished row disappeared")
        _assert_equal("queued finished snapshot status", queued_finished_snapshot.status, JobStatus.ERROR)
        store.quarantine_invalid_jobs()
        cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=100,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "queued finished cleanup candidate after quarantine",
            "queued_finished" in [job.job_id for job in cleanup_jobs],
            True,
        )

        done_null_finished = _job("done_null_finished", root, status=JobStatus.DONE)
        if not store.enqueue(done_null_finished, max_queue_size=10):
            raise AssertionError("enqueue failed for done_null_finished")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET finished_at_s = ? WHERE job_id = ?;",
                    (None, "done_null_finished"),
                )
        done_null_snapshot = store.get_snapshot("done_null_finished")
        if done_null_snapshot is None:
            raise AssertionError("done null-finished row disappeared")
        _assert_equal("done null-finished snapshot status", done_null_snapshot.status, JobStatus.ERROR)
        store.quarantine_invalid_jobs()
        cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=100,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "done null-finished cleanup candidate after quarantine",
            "done_null_finished" in [job.job_id for job in cleanup_jobs],
            True,
        )

        done_non_monotonic_clock = _job("done_non_monotonic_clock", root, status=JobStatus.DONE)
        if not store.enqueue(done_non_monotonic_clock, max_queue_size=10):
            raise AssertionError("enqueue failed for done_non_monotonic_clock")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET created_at_s = ?, started_at_s = ?, finished_at_s = ? WHERE job_id = ?;",
                    (100.0, 90.0, 50.0, "done_non_monotonic_clock"),
                )
        non_monotonic_snapshot = store.get_snapshot("done_non_monotonic_clock")
        if non_monotonic_snapshot is None:
            raise AssertionError("non-monotonic timestamp row disappeared")
        _assert_equal("non-monotonic timestamp status", non_monotonic_snapshot.status, JobStatus.DONE)
        _assert_equal("non-monotonic timestamp reward", non_monotonic_snapshot.reward, 1.0)
        _assert_equal("non-monotonic timestamp msg", non_monotonic_snapshot.msg, "")
        with closing(sqlite3.connect(store.db_path)) as conn:
            non_monotonic_row = conn.execute(
                "SELECT created_at_s, started_at_s, finished_at_s, updated_at_s FROM jobs WHERE job_id = ?;",
                ("done_non_monotonic_clock",),
            ).fetchone()
        if non_monotonic_row is None:
            raise AssertionError("non-monotonic timestamp persisted row disappeared")
        _assert_equal("non-monotonic timestamp persisted created_at", non_monotonic_row[0], 100.0)
        _assert_equal("non-monotonic timestamp persisted started_at", non_monotonic_row[1], 90.0)
        _assert_equal("non-monotonic timestamp persisted finished_at", non_monotonic_row[2], 50.0)
        first_updated_at = non_monotonic_row[3]
        second_non_monotonic_snapshot = store.get_snapshot("done_non_monotonic_clock")
        if second_non_monotonic_snapshot is None:
            raise AssertionError("non-monotonic timestamp second snapshot disappeared")
        with closing(sqlite3.connect(store.db_path)) as conn:
            second_updated_at = conn.execute(
                "SELECT updated_at_s FROM jobs WHERE job_id = ?;",
                ("done_non_monotonic_clock",),
            ).fetchone()[0]
        _assert_equal("non-monotonic timestamp is not rewritten", second_updated_at, first_updated_at)

        recalc_non_monotonic_clock = JobRecord(
            job_id="recalc_non_monotonic_clock",
            thread_dir="recalculate",
            gt_file=root / "recalc_jobs",
            proc_file=root / "recalc_jobs" / "recalc_non_monotonic_clock" / "workbook.xlsx",
            answer_position="",
            kind=JobKind.RECALCULATE,
            status=JobStatus.DONE,
            created_at_s=100.0,
            started_at_s=90.0,
            finished_at_s=50.0,
            reward=0.0,
        )
        if not store.enqueue(recalc_non_monotonic_clock, max_queue_size=10):
            raise AssertionError("enqueue failed for recalc_non_monotonic_clock")
        recalc_non_monotonic_snapshot = store.get_snapshot("recalc_non_monotonic_clock")
        if recalc_non_monotonic_snapshot is None:
            raise AssertionError("non-monotonic recalc timestamp row disappeared")
        _assert_equal("non-monotonic recalc timestamp kind", recalc_non_monotonic_snapshot.kind, JobKind.RECALCULATE)
        _assert_equal("non-monotonic recalc timestamp status", recalc_non_monotonic_snapshot.status, JobStatus.DONE)
        _assert_equal("non-monotonic recalc timestamp msg", recalc_non_monotonic_snapshot.msg, "")
        early_recalc_cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=75.0,
            batch_size=100,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "non-monotonic recalc cleanup uses later timestamp",
            "recalc_non_monotonic_clock" in [job.job_id for job in early_recalc_cleanup_jobs],
            False,
        )
        late_recalc_cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=101.0,
            batch_size=100,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "non-monotonic recalc cleanup eventually eligible",
            "recalc_non_monotonic_clock" in [job.job_id for job in late_recalc_cleanup_jobs],
            True,
        )

        clock_claim_store = SqliteJobStore(root / "clock_claim.sqlite3")
        cleanup.callback(clock_claim_store.close)
        clock_claim_store.init()
        running_non_monotonic_clock = _job(
            "running_non_monotonic_clock",
            root,
            status=JobStatus.RUNNING,
        )
        queued_after_non_monotonic_clock = _job("queued_after_non_monotonic_clock", root)
        if not clock_claim_store.enqueue(running_non_monotonic_clock, max_queue_size=10):
            raise AssertionError("enqueue failed for running_non_monotonic_clock")
        _set_worker_id(clock_claim_store, "running_non_monotonic_clock", "clock-worker")
        if not clock_claim_store.enqueue(queued_after_non_monotonic_clock, max_queue_size=10):
            raise AssertionError("enqueue failed for queued_after_non_monotonic_clock")
        running_clock_now = time.time()
        with closing(sqlite3.connect(clock_claim_store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET created_at_s = ?, started_at_s = ? WHERE job_id = ?;",
                    (running_clock_now - 10.0, running_clock_now - 120.0, "running_non_monotonic_clock"),
                )
        running_non_monotonic_snapshot = clock_claim_store.get_snapshot("running_non_monotonic_clock")
        if running_non_monotonic_snapshot is None:
            raise AssertionError("running non-monotonic timestamp row disappeared")
        _assert_equal(
            "running non-monotonic timestamp status",
            running_non_monotonic_snapshot.status,
            JobStatus.RUNNING,
        )
        _assert_equal(
            "running non-monotonic timestamp blocks slot",
            clock_claim_store.claim_next(worker_id="worker", max_running_jobs=1),
            None,
        )
        _assert_equal(
            "running non-monotonic timestamp not stale by skewed start",
            clock_claim_store.mark_stale_running_as_error(older_than_s=60.0, msg="stale"),
            0,
        )
        running_non_monotonic_snapshot = clock_claim_store.get_snapshot("running_non_monotonic_clock")
        if running_non_monotonic_snapshot is None:
            raise AssertionError("running non-monotonic timestamp row disappeared after stale check")
        _assert_equal(
            "running non-monotonic timestamp still running",
            running_non_monotonic_snapshot.status,
            JobStatus.RUNNING,
        )
        _assert_equal(
            "running non-monotonic timestamp eventually stale",
            clock_claim_store.mark_stale_running_as_error(older_than_s=1.0, msg="stale"),
            1,
        )
        claimed_after_non_monotonic_stale = clock_claim_store.claim_next(
            worker_id="worker",
            max_running_jobs=1,
        )
        if claimed_after_non_monotonic_stale is None:
            raise AssertionError("stale non-monotonic running row still blocked claim_next")
        _assert_equal(
            "claim after stale non-monotonic running row",
            claimed_after_non_monotonic_stale.job_id,
            "queued_after_non_monotonic_clock",
        )

        old_recalc_timestamp_quarantine_kept_error = JobRecord(
            job_id="old_recalc_timestamp_quarantine_kept_error",
            thread_dir="recalculate",
            gt_file=root / "recalc_jobs",
            proc_file=root / "recalc_jobs" / "old_recalc_timestamp_quarantine_kept_error" / "workbook.xlsx",
            answer_position="",
            kind=JobKind.RECALCULATE,
            status=JobStatus.ERROR,
            created_at_s=100.0,
            started_at_s=90.0,
            finished_at_s=50.0,
            reward=0.0,
            msg="invalid persisted job row: started_at_s=invalid, finished_at_s=invalid",
        )
        if not store.enqueue(old_recalc_timestamp_quarantine_kept_error, max_queue_size=10):
            raise AssertionError("enqueue failed for old_recalc_timestamp_quarantine_kept_error")
        old_recalc_snapshot = store.get_snapshot("old_recalc_timestamp_quarantine_kept_error")
        if old_recalc_snapshot is None:
            raise AssertionError("old recalc timestamp quarantine row disappeared")
        _assert_equal("old recalc timestamp quarantine kind", old_recalc_snapshot.kind, JobKind.RECALCULATE)
        _assert_equal("old recalc timestamp quarantine status", old_recalc_snapshot.status, JobStatus.ERROR)
        _assert_equal(
            "old recalc timestamp quarantine msg",
            old_recalc_snapshot.msg,
            "invalid persisted job row: started_at_s=invalid, finished_at_s=invalid",
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            old_recalc_row = conn.execute(
                "SELECT status, msg FROM jobs WHERE job_id = ?;",
                ("old_recalc_timestamp_quarantine_kept_error",),
            ).fetchone()
        if old_recalc_row is None:
            raise AssertionError("old recalc timestamp quarantine persisted row disappeared")
        _assert_equal("old recalc timestamp quarantine persisted status", old_recalc_row[0], JobStatus.ERROR.value)
        _assert_equal(
            "old recalc timestamp quarantine persisted msg",
            old_recalc_row[1],
            "invalid persisted job row: started_at_s=invalid, finished_at_s=invalid",
        )

        old_recalc_finished_quarantine_kept_error = JobRecord(
            job_id="old_recalc_finished_quarantine_kept_error",
            thread_dir="recalculate",
            gt_file=root / "recalc_jobs",
            proc_file=root / "recalc_jobs" / "old_recalc_finished_quarantine_kept_error" / "workbook.xlsx",
            answer_position="",
            kind=JobKind.RECALCULATE,
            status=JobStatus.ERROR,
            created_at_s=100.0,
            started_at_s=90.0,
            finished_at_s=100.0,
            reward=0.0,
            msg="invalid persisted job row: finished_at_s=invalid",
        )
        if not store.enqueue(old_recalc_finished_quarantine_kept_error, max_queue_size=10):
            raise AssertionError("enqueue failed for old_recalc_finished_quarantine_kept_error")
        old_recalc_finished_snapshot = store.get_snapshot("old_recalc_finished_quarantine_kept_error")
        if old_recalc_finished_snapshot is None:
            raise AssertionError("old recalc finished quarantine row disappeared")
        _assert_equal(
            "old recalc finished quarantine status",
            old_recalc_finished_snapshot.status,
            JobStatus.ERROR,
        )
        _assert_equal(
            "old recalc finished quarantine msg",
            old_recalc_finished_snapshot.msg,
            "invalid persisted job row: finished_at_s=invalid",
        )

        quarantined_non_monotonic = _job("quarantined_non_monotonic", root, status=JobStatus.ERROR)
        if not store.enqueue(quarantined_non_monotonic, max_queue_size=10):
            raise AssertionError("enqueue failed for quarantined_non_monotonic")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET thread_dir = ?,
                        msg = ?,
                        reward = ?,
                        created_at_s = ?,
                        started_at_s = ?,
                        finished_at_s = ?,
                        updated_at_s = ?
                    WHERE job_id = ?;
                    """,
                    (
                        "",
                        "invalid persisted job row: thread_dir=invalid",
                        0.0,
                        100.0,
                        90.0,
                        50.0,
                        123.0,
                        "quarantined_non_monotonic",
                    ),
                )
        quarantined_non_monotonic_snapshot = store.get_snapshot("quarantined_non_monotonic")
        if quarantined_non_monotonic_snapshot is None:
            raise AssertionError("quarantined non-monotonic row disappeared")
        _assert_equal(
            "quarantined non-monotonic status",
            quarantined_non_monotonic_snapshot.status,
            JobStatus.ERROR,
        )
        if "thread_dir=invalid" not in quarantined_non_monotonic_snapshot.msg:
            raise AssertionError(
                f"quarantined non-monotonic diagnostic changed: {quarantined_non_monotonic_snapshot.msg!r}"
            )
        with closing(sqlite3.connect(store.db_path)) as conn:
            first_quarantined_updated_at = conn.execute(
                "SELECT updated_at_s FROM jobs WHERE job_id = ?;",
                ("quarantined_non_monotonic",),
            ).fetchone()[0]
        store.get_snapshot("quarantined_non_monotonic")
        with closing(sqlite3.connect(store.db_path)) as conn:
            second_quarantined_updated_at = conn.execute(
                "SELECT updated_at_s FROM jobs WHERE job_id = ?;",
                ("quarantined_non_monotonic",),
            ).fetchone()[0]
        _assert_equal(
            "quarantined non-monotonic row is not rewritten",
            second_quarantined_updated_at,
            first_quarantined_updated_at,
        )

        for empty_field in ("thread_dir", "gt_file", "proc_file"):
            bad_path_job_id = f"bad_empty_{empty_field}"
            bad_path_job = _job(bad_path_job_id, root, status=JobStatus.DONE)
            if not store.enqueue(bad_path_job, max_queue_size=10):
                raise AssertionError(f"enqueue failed for {bad_path_job_id}")
            with closing(sqlite3.connect(store.db_path)) as conn:
                with conn:
                    conn.execute(
                        f"UPDATE jobs SET {empty_field} = ? WHERE job_id = ?;",
                        ("", bad_path_job_id),
                    )
            bad_path_snapshot = store.get_snapshot(bad_path_job_id)
            if bad_path_snapshot is None:
                raise AssertionError(f"{bad_path_job_id} row disappeared")
            _assert_equal(
                f"empty {empty_field} snapshot status",
                bad_path_snapshot.status,
                JobStatus.ERROR,
            )
            _assert_equal(
                f"empty {empty_field} snapshot quarantine flag",
                bad_path_snapshot.quarantined_invalid,
                True,
            )
            _assert_equal(f"empty {empty_field} snapshot reward", bad_path_snapshot.reward, 0.0)
            cleanup_jobs = store.list_cleanup_batch(
                cutoff_s=time.time() + 1.0,
                batch_size=100,
                retry_batch_share=0.5,
            )
            _assert_equal(
                f"empty {empty_field} cleanup candidate",
                bad_path_job_id in [job.job_id for job in cleanup_jobs],
                True,
            )

        bad_nul_thread = _job("bad_nul_thread", root, status=JobStatus.DONE)
        if not store.enqueue(bad_nul_thread, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_nul_thread")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET thread_dir = ? WHERE job_id = ?;",
                    ("thread\x00bad", "bad_nul_thread"),
                )
        bad_nul_thread_snapshot = store.get_snapshot("bad_nul_thread")
        if bad_nul_thread_snapshot is None:
            raise AssertionError("bad_nul_thread row disappeared")
        _assert_equal("nul thread_dir snapshot status", bad_nul_thread_snapshot.status, JobStatus.ERROR)
        _assert_equal("nul thread_dir quarantine flag", bad_nul_thread_snapshot.quarantined_invalid, True)
        _assert_equal("nul thread_dir reward", bad_nul_thread_snapshot.reward, 0.0)
        if "thread_dir=invalid" not in bad_nul_thread_snapshot.msg:
            raise AssertionError(f"nul thread_dir diagnostic missing: {bad_nul_thread_snapshot.msg!r}")

        bad_empty_answer = _job("bad_empty_answer", root, status=JobStatus.DONE)
        if not store.enqueue(bad_empty_answer, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_empty_answer")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET answer_position = ? WHERE job_id = ?;",
                    ("", "bad_empty_answer"),
                )
        bad_empty_answer_snapshot = store.get_snapshot("bad_empty_answer")
        if bad_empty_answer_snapshot is None:
            raise AssertionError("bad_empty_answer row disappeared")
        _assert_equal("empty answer_position snapshot status", bad_empty_answer_snapshot.status, JobStatus.ERROR)
        _assert_equal("empty answer_position quarantine flag", bad_empty_answer_snapshot.quarantined_invalid, True)
        _assert_equal("empty answer_position reward", bad_empty_answer_snapshot.reward, 0.0)
        if "answer_position=invalid" not in bad_empty_answer_snapshot.msg:
            raise AssertionError(
                f"empty answer_position diagnostic missing: {bad_empty_answer_snapshot.msg!r}"
            )

        bad_blob_proc = _job("bad_blob_proc", root, status=JobStatus.DONE)
        if not store.enqueue(bad_blob_proc, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_blob_proc")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET proc_file = ? WHERE job_id = ?;",
                    (sqlite3.Binary(b"abc"), "bad_blob_proc"),
                )
        bad_blob_snapshot = store.get_snapshot("bad_blob_proc")
        if bad_blob_snapshot is None:
            raise AssertionError("bad_blob_proc row disappeared")
        _assert_equal("blob proc_file snapshot status", bad_blob_snapshot.status, JobStatus.ERROR)
        _assert_equal("blob proc_file quarantine flag", bad_blob_snapshot.quarantined_invalid, True)
        if "proc_file=invalid" not in bad_blob_snapshot.msg:
            raise AssertionError(f"blob proc_file diagnostic missing: {bad_blob_snapshot.msg!r}")

        bad_nul_gt = _job("bad_nul_gt", root, status=JobStatus.DONE)
        if not store.enqueue(bad_nul_gt, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_nul_gt")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET gt_file = ? WHERE job_id = ?;",
                    (f"{root}\x00target.xlsx", "bad_nul_gt"),
                )
        bad_nul_snapshot = store.get_snapshot("bad_nul_gt")
        if bad_nul_snapshot is None:
            raise AssertionError("bad_nul_gt row disappeared")
        _assert_equal("nul gt_file snapshot status", bad_nul_snapshot.status, JobStatus.ERROR)
        _assert_equal("nul gt_file quarantine flag", bad_nul_snapshot.quarantined_invalid, True)
        if "gt_file=invalid" not in bad_nul_snapshot.msg:
            raise AssertionError(f"nul gt_file diagnostic missing: {bad_nul_snapshot.msg!r}")

        bad_answer_blob = _job("bad_answer_blob", root)
        if not store.enqueue(bad_answer_blob, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_answer_blob")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET answer_position = ? WHERE job_id = ?;",
                    (sqlite3.Binary(b"Sheet1!A1"), "bad_answer_blob"),
                )
        bad_answer_snapshot = store.get_snapshot("bad_answer_blob")
        if bad_answer_snapshot is None:
            raise AssertionError("bad_answer_blob row disappeared")
        _assert_equal("blob answer_position snapshot status", bad_answer_snapshot.status, JobStatus.ERROR)
        _assert_equal("blob answer_position quarantine flag", bad_answer_snapshot.quarantined_invalid, True)
        if "answer_position=invalid" not in bad_answer_snapshot.msg:
            raise AssertionError(f"blob answer_position diagnostic missing: {bad_answer_snapshot.msg!r}")

        bad_running_numeric = _job("bad_running_numeric", root, status=JobStatus.RUNNING)
        queued_after_bad_running = _job("queued_after_bad_running", root)
        if not store.enqueue(bad_running_numeric, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_running_numeric")
        if not store.enqueue(queued_after_bad_running, max_queue_size=10):
            raise AssertionError("enqueue failed for queued_after_bad_running")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET started_at_s = ? WHERE job_id = ?;",
                    ("bad-started-at", "bad_running_numeric"),
                )
        if store.quarantine_invalid_jobs() < 1:
            raise AssertionError("explicit quarantine did not repair corrupt running row")
        bad_running_snapshot = store.get_snapshot("bad_running_numeric")
        if bad_running_snapshot is None:
            raise AssertionError("bad running numeric job disappeared")
        _assert_equal("bad running numeric status", bad_running_snapshot.status, JobStatus.ERROR)
        claimed_after_bad_running = store.claim_next(worker_id="worker", max_running_jobs=1)
        if claimed_after_bad_running is None:
            raise AssertionError("corrupt running row still blocked claim_next")
        _assert_equal("claim after corrupt running row", claimed_after_bad_running.job_id, "queued_after_bad_running")
        store.finish(
            job_id=claimed_after_bad_running.job_id,
            status=JobStatus.DONE,
            reward=1.0,
            msg="",
        )

        bad_finished_numeric = _job("bad_finished_numeric", root, status=JobStatus.DONE)
        if not store.enqueue(bad_finished_numeric, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_finished_numeric")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE jobs SET finished_at_s = ? WHERE job_id = ?;",
                    ("bad-finished-at", "bad_finished_numeric"),
                )
        store.quarantine_invalid_jobs()
        cleanup_jobs = store.list_cleanup_batch(
            cutoff_s=time.time() + 1.0,
            batch_size=100,
            retry_batch_share=0.5,
        )
        _assert_equal(
            "bad finished numeric cleanup candidate",
            "bad_finished_numeric" in [job.job_id for job in cleanup_jobs],
            True,
        )

        bad_recalc_status = JobRecord(
            job_id="bad_recalc_status",
            thread_dir="recalculate",
            gt_file=root / "recalc.xlsx",
            proc_file=root / "recalc.xlsx",
            answer_position="",
            kind=JobKind.RECALCULATE,
            status=JobStatus.QUEUED,
        )
        if not store.enqueue(bad_recalc_status, max_queue_size=10):
            raise AssertionError("enqueue failed for bad_recalc_status")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?;", ("bogus", "bad_recalc_status"))
        bad_recalc_status_snapshot = store.get_snapshot("bad_recalc_status")
        if bad_recalc_status_snapshot is None:
            raise AssertionError("bad recalc status job disappeared")
        _assert_equal("invalid recalc status snapshot kind", bad_recalc_status_snapshot.kind, JobKind.RECALCULATE)
        _assert_equal("invalid recalc status snapshot status", bad_recalc_status_snapshot.status, JobStatus.ERROR)
        store.quarantine_invalid_jobs()
        with closing(sqlite3.connect(store.db_path)) as conn:
            row = conn.execute(
                "SELECT kind, status FROM jobs WHERE job_id = ?;",
                ("bad_recalc_status",),
            ).fetchone()
        if row is None:
            raise AssertionError("bad recalc status row disappeared")
        _assert_equal("invalid recalc status persisted kind", row[0], JobKind.RECALCULATE.value)
        _assert_equal("invalid recalc status persisted status", row[1], JobStatus.ERROR.value)

    print("OK: job store counters look good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
