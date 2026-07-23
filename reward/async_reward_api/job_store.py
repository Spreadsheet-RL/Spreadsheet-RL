from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from .models import JobKind, JobRecord, JobStatus

_MAX_PERSISTED_NUMERIC_ABS = 1.0e100
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
_CLEANUP_RETENTION_SQL = "CASE WHEN created_at_s > finished_at_s THEN created_at_s ELSE finished_at_s END"
_RUNNING_STALE_AGE_SQL = "CASE WHEN created_at_s > started_at_s THEN created_at_s ELSE started_at_s END"


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    proc_file: Path
    kind: JobKind
    status: JobStatus
    created_at_s: float
    started_at_s: float | None
    finished_at_s: float | None
    reward: float | None
    msg: str
    gt_file: Path | None = None
    quarantined_invalid: bool = False


@dataclass(frozen=True)
class CleanupCandidate:
    job_id: str
    kind: JobKind
    proc_file: Path
    gt_file: Path
    quarantined_invalid: bool = False


def _get_sqlite_timeout_s() -> float:
    value = os.environ.get("REWARD_API_SQLITE_TIMEOUT_S", "30")
    try:
        timeout_s = float(value)
    except ValueError:
        return 30.0
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        return 30.0
    return min(timeout_s, 300.0)


class SqliteJobStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        self._closed = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    def init(self) -> None:
        with self._connections_lock:
            self._closed = False
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
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
                    updated_at_s REAL NOT NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs);").fetchall()}
            if "kind" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'reward';")
            if "worker_id" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN worker_id TEXT;")
            if "cleanup_next_retry_s" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN cleanup_next_retry_s REAL;")
            if "cleanup_attempts" not in columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN cleanup_attempts INTEGER NOT NULL DEFAULT 0;"
                )
            conn.execute("DROP INDEX IF EXISTS idx_jobs_status_created;")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_job_id ON jobs(status, created_at_s, job_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_started ON jobs(status, started_at_s);"
            )
            conn.execute("DROP INDEX IF EXISTS idx_jobs_finished;")
            conn.execute("DROP INDEX IF EXISTS idx_jobs_finished_job_id;")
            conn.execute("DROP INDEX IF EXISTS idx_jobs_cleanup_retry;")
            conn.execute("DROP INDEX IF EXISTS idx_jobs_status_finished_job_id;")
            conn.execute("DROP INDEX IF EXISTS idx_jobs_status_cleanup_retry;")
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_jobs_status_cleanup_retention_job_id
                ON jobs(status, ({_CLEANUP_RETENTION_SQL}), job_id)
                WHERE cleanup_next_retry_s IS NULL AND finished_at_s IS NOT NULL;
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_jobs_status_cleanup_retry_retention_job_id
                ON jobs(status, cleanup_next_retry_s, ({_CLEANUP_RETENTION_SQL}), job_id)
                WHERE cleanup_next_retry_s IS NOT NULL AND finished_at_s IS NOT NULL;
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                """
                INSERT INTO job_counters(name, value)
                VALUES (?, 0)
                ON CONFLICT(name) DO NOTHING;
                """,
                [("queued",), ("running",), ("total",)],
            )
            for trigger_name in (
                "trg_jobs_counter_insert_queued",
                "trg_jobs_counter_insert_running",
                "trg_jobs_counter_insert_total",
                "trg_jobs_counter_update_queued",
                "trg_jobs_counter_update_running",
                "trg_jobs_counter_delete_queued",
                "trg_jobs_counter_delete_running",
                "trg_jobs_counter_delete_total",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name};")
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_insert_queued
                AFTER INSERT ON jobs
                WHEN NEW.status = 'queued'
                BEGIN
                    UPDATE job_counters
                    SET value = value + 1
                    WHERE name = 'queued';
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_insert_running
                AFTER INSERT ON jobs
                WHEN NEW.status = 'running'
                BEGIN
                    UPDATE job_counters
                    SET value = value + 1
                    WHERE name = 'running';
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_insert_total
                AFTER INSERT ON jobs
                BEGIN
                    UPDATE job_counters
                    SET value = value + 1
                    WHERE name = 'total';
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_update_queued
                AFTER UPDATE OF status ON jobs
                WHEN OLD.status <> NEW.status
                BEGIN
                    UPDATE job_counters
                    SET value = value
                        + CASE WHEN NEW.status = 'queued' THEN 1 ELSE 0 END
                        - CASE WHEN OLD.status = 'queued' THEN 1 ELSE 0 END
                    WHERE name = 'queued';
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_update_running
                AFTER UPDATE OF status ON jobs
                WHEN OLD.status <> NEW.status
                BEGIN
                    UPDATE job_counters
                    SET value = value
                        + CASE WHEN NEW.status = 'running' THEN 1 ELSE 0 END
                        - CASE WHEN OLD.status = 'running' THEN 1 ELSE 0 END
                    WHERE name = 'running';
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_delete_queued
                AFTER DELETE ON jobs
                WHEN OLD.status = 'queued'
                BEGIN
                    UPDATE job_counters
                    SET value = value - 1
                    WHERE name = 'queued';
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_delete_running
                AFTER DELETE ON jobs
                WHEN OLD.status = 'running'
                BEGIN
                    UPDATE job_counters
                    SET value = value - 1
                    WHERE name = 'running';
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER trg_jobs_counter_delete_total
                AFTER DELETE ON jobs
                BEGIN
                    UPDATE job_counters
                    SET value = value - 1
                    WHERE name = 'total';
                END;
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_leases (
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_until_s REAL NOT NULL,
                    updated_at_s REAL NOT NULL
                );
                """
            )
            self._quarantine_invalid_rows(conn)
            conn.execute(
                """
                UPDATE job_counters
                SET value = (
                    SELECT COUNT(*) FROM jobs WHERE status = ?
                )
                WHERE name = 'queued';
                """,
                (JobStatus.QUEUED.value,),
            )
            conn.execute(
                """
                UPDATE job_counters
                SET value = (
                    SELECT COUNT(*) FROM jobs WHERE status = ?
                )
                WHERE name = 'running';
                """,
                (JobStatus.RUNNING.value,),
            )
            conn.execute(
                """
                UPDATE job_counters
                SET value = (
                    SELECT COUNT(*) FROM jobs
                )
                WHERE name = 'total';
                """
            )

    def try_acquire_maintenance_lease(
        self,
        *,
        name: str,
        owner_id: str,
        lease_s: float,
        now_s: float | None = None,
        allow_owner_renewal: bool = True,
    ) -> bool:
        lease_name = name.strip()
        owner = owner_id.strip()
        if not lease_name:
            raise ValueError("lease name cannot be empty")
        if not owner:
            raise ValueError("owner_id cannot be empty")
        now = time.time() if now_s is None else float(now_s)
        lease_until = now + max(1.0, float(lease_s))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                """
                SELECT owner_id, lease_until_s
                FROM maintenance_leases
                WHERE name = ?;
                """,
                (lease_name,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO maintenance_leases(name, owner_id, lease_until_s, updated_at_s)
                    VALUES (?, ?, ?, ?);
                    """,
                    (lease_name, owner, lease_until, now),
                )
                conn.commit()
                return True

            current_owner_raw = row["owner_id"]
            current_owner = current_owner_raw if isinstance(current_owner_raw, str) else ""
            try:
                current_until = float(row["lease_until_s"])
            except (TypeError, ValueError):
                current_until = 0.0
            if not math.isfinite(current_until) or abs(current_until) > _MAX_PERSISTED_NUMERIC_ABS:
                current_until = 0.0
            if not _required_persisted_text_is_valid(current_owner_raw):
                current_until = 0.0
            if (allow_owner_renewal and current_owner == owner) or current_until <= now:
                conn.execute(
                    """
                    UPDATE maintenance_leases
                    SET owner_id = ?, lease_until_s = ?, updated_at_s = ?
                    WHERE name = ?;
                    """,
                    (owner, lease_until, now, lease_name),
                )
                conn.commit()
                return True

            conn.commit()
            return False

    def _get_counter(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        fallback_sql: str,
        fallback_params: tuple[object, ...] = (),
    ) -> int:
        row = conn.execute(
            "SELECT value FROM job_counters WHERE name = ?;",
            (name,),
        ).fetchone()
        if row is not None:
            value = self._coerce_counter_value(row[0])
            if value is not None:
                return value
        return self._repair_counter(
            conn,
            name=name,
            fallback_sql=fallback_sql,
            fallback_params=fallback_params,
        )

    @staticmethod
    def _coerce_counter_value(raw_value: object) -> int | None:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = -1
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            numeric_value = -1.0
        if (
            value >= 0
            and value <= _MAX_SQLITE_INTEGER
            and math.isfinite(numeric_value)
            and abs(numeric_value) <= _MAX_PERSISTED_NUMERIC_ABS
            and float(value) == numeric_value
        ):
            return value
        return None

    def _repair_counter(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        fallback_sql: str,
        fallback_params: tuple[object, ...] = (),
    ) -> int:
        fallback = conn.execute(fallback_sql, fallback_params).fetchone()[0]
        value = int(fallback)
        conn.execute(
            """
            INSERT INTO job_counters(name, value)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value;
            """,
            (name, value),
        )
        return value

    def _get_counters(
        self,
        conn: sqlite3.Connection,
        specs: dict[str, tuple[str, tuple[object, ...]]],
    ) -> dict[str, int]:
        if not specs:
            return {}
        placeholders = ",".join("?" for _ in specs)
        rows = conn.execute(
            f"SELECT name, value FROM job_counters WHERE name IN ({placeholders});",
            tuple(specs),
        ).fetchall()
        raw_values = {str(row["name"]): row["value"] for row in rows}
        counters: dict[str, int] = {}
        for name, (fallback_sql, fallback_params) in specs.items():
            value = None
            if name in raw_values:
                value = self._coerce_counter_value(raw_values[name])
            if value is None:
                value = self._repair_counter(
                    conn,
                    name=name,
                    fallback_sql=fallback_sql,
                    fallback_params=fallback_params,
                )
            counters[name] = value
        return counters

    def _get_queued_count(self, conn: sqlite3.Connection) -> int:
        return self._get_counter(
            conn,
            name="queued",
            fallback_sql="SELECT COUNT(*) FROM jobs WHERE status = ?",
            fallback_params=(JobStatus.QUEUED.value,),
        )

    def _get_running_count(self, conn: sqlite3.Connection) -> int:
        return self._get_counter(
            conn,
            name="running",
            fallback_sql="SELECT COUNT(*) FROM jobs WHERE status = ?",
            fallback_params=(JobStatus.RUNNING.value,),
        )

    def _get_total_count(self, conn: sqlite3.Connection) -> int:
        return self._get_counter(
            conn,
            name="total",
            fallback_sql="SELECT COUNT(*) FROM jobs",
        )

    def has_queue_capacity(self, *, max_queue_size: int) -> bool:
        with self._connection() as conn:
            return self._get_queued_count(conn) < max(1, int(max_queue_size))

    def get_snapshot(self, job_id: str) -> JobSnapshot | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    *
                FROM jobs
                WHERE job_id = ?;
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_snapshot(row)

    def quarantine_invalid_jobs(self) -> int:
        with self._connection() as conn:
            quarantined = self._quarantine_invalid_rows(conn)
            conn.commit()
            return quarantined

    def _quarantine_invalid_rows(self, conn: sqlite3.Connection, *, job_id: str | None = None) -> int:
        now = time.time()
        if job_id is None:
            self._repair_missing_job_ids(conn, now=now)
        valid_kinds = tuple(kind.value for kind in JobKind)
        valid_statuses = tuple(status.value for status in JobStatus)
        numeric_fields = (
            "created_at_s",
            "started_at_s",
            "finished_at_s",
            "reward",
            "cleanup_next_retry_s",
        )
        conditions = [
            "job_id IS NULL",
            "typeof(job_id) != 'text'",
            "TRIM(job_id) = ''",
            "instr(job_id, char(0)) > 0",
            "kind IS NULL",
            f"kind NOT IN ({','.join('?' for _ in valid_kinds)})",
            "status IS NULL",
            f"status NOT IN ({','.join('?' for _ in valid_statuses)})",
            "created_at_s IS NULL",
            "typeof(created_at_s) NOT IN ('integer', 'real')",
            f"(created_at_s < -{_MAX_PERSISTED_NUMERIC_ABS} OR created_at_s > {_MAX_PERSISTED_NUMERIC_ABS})",
            *[
                f"({field} IS NOT NULL AND "
                f"(typeof({field}) NOT IN ('integer', 'real') "
                f"OR {field} < -{_MAX_PERSISTED_NUMERIC_ABS} "
                f"OR {field} > {_MAX_PERSISTED_NUMERIC_ABS}))"
                for field in numeric_fields
                if field != "created_at_s"
            ],
            "thread_dir IS NULL",
            "typeof(thread_dir) != 'text'",
            "TRIM(thread_dir) = ''",
            "instr(thread_dir, char(0)) > 0",
            "gt_file IS NULL",
            "typeof(gt_file) != 'text'",
            "TRIM(gt_file) = ''",
            "instr(gt_file, char(0)) > 0",
            "proc_file IS NULL",
            "typeof(proc_file) != 'text'",
            "TRIM(proc_file) = ''",
            "instr(proc_file, char(0)) > 0",
            "(kind = ? AND (answer_position IS NULL OR typeof(answer_position) != 'text' OR TRIM(answer_position) = '' OR instr(answer_position, char(0)) > 0))",
            "(status = ? AND (started_at_s IS NOT NULL OR finished_at_s IS NOT NULL))",
            "(status = ? AND (started_at_s IS NULL OR finished_at_s IS NOT NULL))",
            "(status = ? AND (worker_id IS NULL OR typeof(worker_id) != 'text' OR TRIM(worker_id) = '' OR instr(worker_id, char(0)) > 0))",
            "(status IN (?, ?) AND finished_at_s IS NULL)",
            "(kind = ? AND status = ? AND reward IS NULL)",
            "cleanup_attempts IS NULL",
            "typeof(cleanup_attempts) NOT IN ('integer', 'real')",
            f"cleanup_attempts > {_MAX_SQLITE_INTEGER}",
            "cleanup_attempts < 0",
            "cleanup_attempts != CAST(cleanup_attempts AS INTEGER)",
        ]
        params: list[object] = [
            *valid_kinds,
            *valid_statuses,
            JobKind.REWARD.value,
            JobStatus.QUEUED.value,
            JobStatus.RUNNING.value,
            JobStatus.RUNNING.value,
            JobStatus.DONE.value,
            JobStatus.ERROR.value,
            JobKind.REWARD.value,
            JobStatus.DONE.value,
        ]
        job_filter = ""
        if job_id is not None:
            job_filter = " AND job_id = ?"
            params.append(job_id)
        rows = conn.execute(
            f"""
            SELECT
                rowid,
                job_id,
                kind,
                status,
                thread_dir,
                gt_file,
                proc_file,
                answer_position,
                worker_id,
                msg,
                created_at_s,
                started_at_s,
                finished_at_s,
                reward,
                cleanup_next_retry_s,
                cleanup_attempts
            FROM jobs
            WHERE ({' OR '.join(conditions)}){job_filter};
            """,
            tuple(params),
        ).fetchall()
        if not rows:
            return 0
        valid_kind_values = {kind.value for kind in JobKind}
        updates = []
        cleanup_updates = []
        for row in rows:
            invalid_reasons = _invalid_persisted_job_reasons(row)
            cleanup_next_retry_invalid = not _row_numeric_value_is_valid(
                row["cleanup_next_retry_s"],
                required=False,
            )
            cleanup_attempts_invalid = not _cleanup_attempts_value_is_valid(row["cleanup_attempts"])
            if cleanup_next_retry_invalid or cleanup_attempts_invalid:
                cleanup_updates.append(
                    (
                        None
                        if cleanup_next_retry_invalid or row["cleanup_next_retry_s"] is None
                        else float(row["cleanup_next_retry_s"]),
                        0 if cleanup_attempts_invalid else int(row["cleanup_attempts"]),
                        now,
                        int(row["rowid"]),
                    )
                )
            if not invalid_reasons:
                continue
            if _is_already_quarantined_invalid_row(row):
                continue
            raw_kind = str(row["kind"])
            kind = raw_kind if raw_kind in valid_kind_values else JobKind.REWARD.value
            created_at_s = (
                float(row["created_at_s"])
                if _row_numeric_value_is_valid(row["created_at_s"], required=True)
                else now
            )
            started_at_s = (
                float(row["started_at_s"])
                if _row_numeric_value_is_valid(row["started_at_s"], required=False)
                and row["started_at_s"] is not None
                else None
            )
            finished_at_s = (
                float(row["finished_at_s"])
                if _row_numeric_value_is_valid(row["finished_at_s"], required=False)
                and row["finished_at_s"] is not None
                else now
            )
            if "started_at_s=invalid" in invalid_reasons:
                started_at_s = None
            if "finished_at_s=invalid" in invalid_reasons:
                finished_at_s = max(created_at_s, now)
            updates.append(
                (
                    kind,
                    JobStatus.ERROR.value,
                    created_at_s,
                    started_at_s,
                    0.0,
                    f"invalid persisted job row: {', '.join(invalid_reasons)}",
                    finished_at_s,
                    now,
                    int(row["rowid"]),
                )
            )
        if cleanup_updates:
            conn.executemany(
                """
                UPDATE jobs
                SET cleanup_next_retry_s = ?,
                    cleanup_attempts = ?,
                    updated_at_s = ?
                WHERE rowid = ?;
                """,
                cleanup_updates,
            )
        conn.executemany(
            """
            UPDATE jobs
            SET kind = ?,
                status = ?,
                created_at_s = ?,
                started_at_s = ?,
                reward = ?,
                msg = ?,
                finished_at_s = ?,
                updated_at_s = ?
            WHERE rowid = ?;
            """,
            updates,
        )
        return len(updates) + len(cleanup_updates)

    def _repair_missing_job_ids(self, conn: sqlite3.Connection, *, now: float) -> int:
        rows = conn.execute(
            """
            SELECT rowid
            FROM jobs
            WHERE job_id IS NULL
               OR typeof(job_id) != 'text'
               OR TRIM(job_id) = ''
               OR instr(job_id, char(0)) > 0;
            """
        ).fetchall()
        updates = []
        for row in rows:
            rowid = int(row["rowid"])
            suffix = 0
            while True:
                repaired_job_id = f"invalid-{rowid}" if suffix == 0 else f"invalid-{rowid}-{suffix}"
                collision = conn.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?;",
                    (repaired_job_id,),
                ).fetchone()
                if collision is None:
                    updates.append(
                        (
                            repaired_job_id,
                            JobStatus.ERROR.value,
                            0.0,
                            "invalid persisted job row: job_id=invalid",
                            now,
                            now,
                            rowid,
                        )
                    )
                    break
                suffix += 1
        if not updates:
            return 0
        conn.executemany(
            """
            UPDATE jobs
            SET job_id = ?,
                status = ?,
                reward = ?,
                msg = ?,
                finished_at_s = ?,
                updated_at_s = ?
            WHERE rowid = ?;
            """,
            updates,
        )
        return len(updates)

    def enqueue(self, job: JobRecord, *, max_queue_size: int) -> bool:
        with self._connection() as conn:
            # Serialize cross-process queue admission checks. In SQLite, `BEGIN IMMEDIATE`
            # guarantees a single writer at a time, making the size check + insert atomic.
            conn.execute("BEGIN IMMEDIATE")
            queued = self._get_queued_count(conn)
            if queued >= max_queue_size:
                conn.rollback()
                return False

            now = time.time()
            payload = asdict(job)
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
                    worker_id,
                    updated_at_s
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    payload["job_id"],
                    payload["thread_dir"],
                    str(payload["gt_file"]),
                    str(payload["proc_file"]),
                    payload["answer_position"],
                    payload["kind"].value if isinstance(payload["kind"], JobKind) else str(payload["kind"]),
                    payload["status"].value if isinstance(payload["status"], JobStatus) else str(payload["status"]),
                    float(payload["created_at_s"]),
                    payload["started_at_s"],
                    payload["finished_at_s"],
                    payload["reward"],
                    payload["msg"] or "",
                    None,
                    now,
                ),
            )
            conn.commit()
            return True

    def _claim_next_returning(
        self,
        conn: sqlite3.Connection,
        *,
        now: float,
        worker_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            UPDATE jobs
            SET status = ?, started_at_s = ?, worker_id = ?, updated_at_s = ?
            WHERE rowid = (
                SELECT rowid
                FROM jobs
                WHERE status = ?
                ORDER BY created_at_s, job_id
                LIMIT 1
            ) AND status = ?
            RETURNING *;
            """,
            (
                JobStatus.RUNNING.value,
                now,
                worker_id,
                now,
                JobStatus.QUEUED.value,
                JobStatus.QUEUED.value,
            ),
        ).fetchone()

    def _claim_next_legacy(
        self,
        conn: sqlite3.Connection,
        *,
        now: float,
        worker_id: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT rowid FROM jobs
            WHERE status = ?
            ORDER BY created_at_s, job_id
            LIMIT 1;
            """,
            (JobStatus.QUEUED.value,),
        ).fetchone()
        if row is None:
            return None

        rowid = int(row["rowid"])
        updated = conn.execute(
            """
            UPDATE jobs
            SET status = ?, started_at_s = ?, worker_id = ?, updated_at_s = ?
            WHERE rowid = ? AND status = ?;
            """,
            (
                JobStatus.RUNNING.value,
                now,
                worker_id,
                now,
                rowid,
                JobStatus.QUEUED.value,
            ),
        ).rowcount
        if updated != 1:
            return None

        return conn.execute("SELECT * FROM jobs WHERE rowid = ?", (rowid,)).fetchone()

    def claim_next(
        self,
        *,
        worker_id: str,
        max_running_jobs: int | None,
    ) -> JobRecord | None:
        with self._connection() as conn:
            now = time.time()
            has_explicit_tx = False

            if max_running_jobs is not None:
                # Serialize cross-process job claiming only when we need the
                # running-cap check + queued->running claim to be atomic.
                conn.execute("BEGIN IMMEDIATE")
                has_explicit_tx = True
            if max_running_jobs is not None:
                if self._get_running_count(conn) >= max_running_jobs:
                    conn.rollback()
                    return None

            try:
                row = self._claim_next_returning(conn, now=now, worker_id=worker_id)
            except sqlite3.OperationalError as exc:
                if "RETURNING" not in str(exc).upper():
                    raise
                if not has_explicit_tx:
                    conn.commit()
                    conn.execute("BEGIN IMMEDIATE")
                    has_explicit_tx = True
                row = self._claim_next_legacy(conn, now=now, worker_id=worker_id)

            if row is None:
                if has_explicit_tx:
                    conn.rollback()
                return None

            try:
                record = _row_to_record(row)
            except ValueError:
                job_id_raw = row["job_id"]
                if _required_persisted_text_is_valid(job_id_raw):
                    self._quarantine_invalid_rows(conn, job_id=str(job_id_raw))
                else:
                    self._quarantine_invalid_rows(conn)
                conn.commit()
                return None

            conn.commit()
            return record

    def requeue_claimed(self, *, job_id: str, worker_id: str) -> bool:
        with self._connection() as conn:
            now = time.time()
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    started_at_s = NULL,
                    worker_id = NULL,
                    updated_at_s = ?
                WHERE job_id = ? AND status = ? AND worker_id = ?;
                """,
                (
                    JobStatus.QUEUED.value,
                    now,
                    job_id,
                    JobStatus.RUNNING.value,
                    worker_id,
                ),
            )
            conn.commit()
            return int(cur.rowcount) == 1

    def finish(
        self,
        *,
        job_id: str,
        status: JobStatus,
        reward: float,
        msg: str,
        worker_id: str | None = None,
    ) -> bool:
        with self._connection() as conn:
            now = time.time()
            try:
                persisted_reward = float(reward)
            except (TypeError, ValueError):
                persisted_reward = 0.0
                invalid_reward_value = True
            else:
                invalid_reward_value = (
                    not math.isfinite(persisted_reward)
                    or abs(persisted_reward) > _MAX_PERSISTED_NUMERIC_ABS
                )
            persisted_status = JobStatus.ERROR if invalid_reward_value else status
            if invalid_reward_value:
                persisted_reward = 0.0
            persisted_msg = msg or ""
            if invalid_reward_value:
                persisted_msg = (
                    f"{persisted_msg}; invalid reward value"
                    if persisted_msg
                    else "invalid reward value"
                )
            worker_guard = ""
            params: tuple[object, ...] = (
                persisted_status.value,
                persisted_reward,
                persisted_msg,
                now,
                now,
                job_id,
                JobStatus.RUNNING.value,
            )
            if worker_id is not None:
                worker_guard = " AND worker_id = ?"
                params = (*params, worker_id)
            cur = conn.execute(
                f"""
                UPDATE jobs
                SET status = ?, reward = ?, msg = ?, finished_at_s = ?, updated_at_s = ?
                WHERE job_id = ? AND status = ?{worker_guard};
                """,
                params,
            )
            conn.commit()
            return int(cur.rowcount) == 1

    def mark_result_expired(self, *, job_id: str, kind: JobKind) -> bool:
        return self.mark_result_error(job_id=job_id, kind=kind, msg="result expired")

    def mark_result_error(self, *, job_id: str, kind: JobKind, msg: str) -> bool:
        with self._connection() as conn:
            now = time.time()
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    reward = 0.0,
                    msg = ?,
                    finished_at_s = COALESCE(finished_at_s, ?),
                    updated_at_s = ?
                WHERE job_id = ? AND kind = ? AND status = ?;
                """,
                (
                    JobStatus.ERROR.value,
                    msg,
                    now,
                    now,
                    job_id,
                    kind.value,
                    JobStatus.DONE.value,
                ),
            )
            conn.commit()
            return int(cur.rowcount) == 1

    def mark_stale_running_as_error(self, *, older_than_s: float, msg: str) -> int:
        with self._connection() as conn:
            now = time.time()
            cur = conn.execute(
                f"""
                UPDATE jobs
                SET status = ?, reward = 0.0, msg = ?, finished_at_s = ?, updated_at_s = ?
                WHERE status = ?
                  AND started_at_s IS NOT NULL
                  AND {_RUNNING_STALE_AGE_SQL} < ?;
                """,
                (
                    JobStatus.ERROR.value,
                    msg or "stale running job",
                    now,
                    now,
                    JobStatus.RUNNING.value,
                    now - float(older_than_s),
                ),
            )
            conn.commit()
            return int(cur.rowcount)

    def _list_finished_before(
        self,
        conn: sqlite3.Connection,
        *,
        cutoff_s: float,
        limit: int | None = None,
        now_s: float,
        mode: str,
    ) -> list[CleanupCandidate]:
        mode_norm = mode.strip().lower()
        if mode_norm not in {"fresh", "retry"}:
            raise ValueError(f"invalid cleanup list mode: {mode}")

        per_status_limit = max(1, int(limit)) if limit is not None else None
        rows: list[sqlite3.Row] = []
        for status in (JobStatus.DONE, JobStatus.ERROR):
            params: list[object]
            if mode_norm == "fresh":
                query = f"""
                    SELECT job_id, kind, gt_file, proc_file, status, msg, created_at_s, finished_at_s, cleanup_next_retry_s
                    FROM jobs
                    WHERE status = ?
                      AND finished_at_s IS NOT NULL
                      AND {_CLEANUP_RETENTION_SQL} < ?
                      AND cleanup_next_retry_s IS NULL
                    ORDER BY {_CLEANUP_RETENTION_SQL}, job_id
                """
                params = [status.value, float(cutoff_s)]
            else:
                query = f"""
                    SELECT job_id, kind, gt_file, proc_file, status, msg, created_at_s, finished_at_s, cleanup_next_retry_s
                    FROM jobs
                    WHERE status = ?
                      AND finished_at_s IS NOT NULL
                      AND {_CLEANUP_RETENTION_SQL} < ?
                      AND cleanup_next_retry_s IS NOT NULL
                      AND cleanup_next_retry_s <= ?
                    ORDER BY cleanup_next_retry_s, {_CLEANUP_RETENTION_SQL}, job_id
                """
                params = [status.value, float(cutoff_s), now_s]
            if per_status_limit is not None:
                query += " LIMIT ?"
                params.append(per_status_limit)
            query += ";"
            rows.extend(conn.execute(query, tuple(params)).fetchall())
        if mode_norm == "retry":
            rows.sort(
                key=lambda row: (
                    float(row["cleanup_next_retry_s"]),
                    _cleanup_retention_s(row),
                    str(row["job_id"]),
                )
            )
        else:
            rows.sort(key=lambda row: (_cleanup_retention_s(row), str(row["job_id"])))
        if per_status_limit is not None:
            rows = rows[:per_status_limit]
        result: list[CleanupCandidate] = []
        for row in rows:
            kind_raw = str(row["kind"] or JobKind.REWARD.value)
            try:
                kind = JobKind(kind_raw)
            except ValueError:
                kind = JobKind.REWARD
            result.append(
                CleanupCandidate(
                    job_id=str(row["job_id"]),
                    kind=kind,
                    proc_file=Path(str(row["proc_file"] or "")),
                    gt_file=Path(str(row["gt_file"] or "")),
                    quarantined_invalid=(
                        str(row["status"] or "") == JobStatus.ERROR.value
                        and str(row["msg"] or "").startswith("invalid persisted job row:")
                    ),
                )
            )
        return result

    def list_cleanup_batch(
        self,
        *,
        cutoff_s: float,
        batch_size: int,
        retry_batch_share: float,
        now_s: float | None = None,
    ) -> list[CleanupCandidate]:
        size = max(1, int(batch_size))
        retry_share = min(max(0.0, float(retry_batch_share)), 1.0)
        retry_target = int(size * retry_share)
        if retry_share > 0.0:
            retry_target = max(1, retry_target)
        retry_target = min(size, retry_target)
        fresh_target = max(0, size - retry_target)

        with self._connection() as conn:
            now = time.time() if now_s is None else float(now_s)
            jobs: list[CleanupCandidate] = []
            seen_job_ids: set[str] = set()

            def _append_unique(candidates: list[CleanupCandidate]) -> None:
                for candidate in candidates:
                    if candidate.job_id in seen_job_ids:
                        continue
                    seen_job_ids.add(candidate.job_id)
                    jobs.append(candidate)
                    if len(jobs) >= size:
                        break

            retry_jobs: list[CleanupCandidate] = []
            fresh_jobs: list[CleanupCandidate] = []
            if retry_target > 0:
                retry_jobs = self._list_finished_before(
                    conn,
                    cutoff_s=cutoff_s,
                    limit=retry_target,
                    now_s=now,
                    mode="retry",
                )
                _append_unique(retry_jobs)
            if len(jobs) < size and fresh_target > 0:
                fresh_jobs = self._list_finished_before(
                    conn,
                    cutoff_s=cutoff_s,
                    limit=fresh_target,
                    now_s=now,
                    mode="fresh",
                )
                _append_unique(fresh_jobs)

            if len(jobs) < size and retry_target > 0 and len(retry_jobs) < retry_target:
                extra_fresh = self._list_finished_before(
                    conn,
                    cutoff_s=cutoff_s,
                    limit=size,
                    now_s=now,
                    mode="fresh",
                )
                _append_unique(extra_fresh)
            if len(jobs) < size and fresh_target > 0 and len(fresh_jobs) < fresh_target:
                extra_retry = self._list_finished_before(
                    conn,
                    cutoff_s=cutoff_s,
                    limit=size,
                    now_s=now,
                    mode="retry",
                )
                _append_unique(extra_retry)
            return jobs

    def mark_cleanup_failed(
        self,
        *,
        job_ids: list[str],
        retry_after_s: float,
        retry_max_s: float,
    ) -> int:
        if not job_ids:
            return 0
        unique_job_ids = list(dict.fromkeys(job_ids))
        base_retry_s = max(1.0, float(retry_after_s))
        max_retry_s = max(base_retry_s, float(retry_max_s))
        now_s = time.time()
        with self._connection() as conn:
            total_updates = 0
            for start in range(0, len(unique_job_ids), 900):
                chunk = unique_job_ids[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT job_id, COALESCE(cleanup_attempts, 0) AS cleanup_attempts
                    FROM jobs
                    WHERE finished_at_s IS NOT NULL AND job_id IN ({placeholders});
                    """,
                    tuple(chunk),
                ).fetchall()
                if not rows:
                    continue

                updates: list[tuple[int, float, str]] = []
                for row in rows:
                    attempts_raw = row["cleanup_attempts"]
                    attempts = int(attempts_raw) if _cleanup_attempts_value_is_valid(attempts_raw) else 0
                    retry_delay_s = min(max_retry_s, base_retry_s * (2 ** min(attempts, 10)))
                    next_attempts = min(_MAX_SQLITE_INTEGER, attempts + 1)
                    updates.append((next_attempts, now_s + retry_delay_s, str(row["job_id"])))

                cur = conn.executemany(
                    """
                    UPDATE jobs
                    SET cleanup_attempts = ?,
                        cleanup_next_retry_s = ?,
                        updated_at_s = ?
                    WHERE job_id = ? AND finished_at_s IS NOT NULL;
                    """,
                    [(attempts, next_retry_s, now_s, job_id) for attempts, next_retry_s, job_id in updates],
                )
                total_updates += int(cur.rowcount)
            conn.commit()
            return total_updates

    def delete_jobs(self, *, job_ids: list[str]) -> int:
        if not job_ids:
            return 0
        with self._connection() as conn:
            deleted = 0
            for start in range(0, len(job_ids), 900):
                chunk = job_ids[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM jobs WHERE job_id IN ({placeholders});",
                    tuple(chunk),
                )
                deleted += int(cur.rowcount)
            conn.commit()
            return deleted

    def stats(self) -> dict[str, int]:
        with self._connection() as conn:
            counters = self._get_counters(
                conn,
                {
                    "queued": (
                        "SELECT COUNT(*) FROM jobs WHERE status = ?",
                        (JobStatus.QUEUED.value,),
                    ),
                    "running": (
                        "SELECT COUNT(*) FROM jobs WHERE status = ?",
                        (JobStatus.RUNNING.value,),
                    ),
                    "total": ("SELECT COUNT(*) FROM jobs", ()),
                },
            )
        return {
            "queued": int(counters["queued"]),
            "running": int(counters["running"]),
            "jobs": int(counters["total"]),
        }

    def has_claimable_jobs(self, max_running_jobs: int | None) -> bool:
        with self._connection() as conn:
            specs = {
                "queued": (
                    "SELECT COUNT(*) FROM jobs WHERE status = ?",
                    (JobStatus.QUEUED.value,),
                ),
            }
            if max_running_jobs is not None:
                specs["running"] = (
                    "SELECT COUNT(*) FROM jobs WHERE status = ?",
                    (JobStatus.RUNNING.value,),
                )
            counters = self._get_counters(conn, specs)
            if counters["queued"] <= 0:
                return False
            if max_running_jobs is not None and counters["running"] >= max_running_jobs:
                return False
            return True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=_get_sqlite_timeout_s(),  # Sets the per-connection busy timeout.
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL;")
        with self._connections_lock:
            if self._closed:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                raise sqlite3.ProgrammingError("job store is closed")
            self._connections.add(conn)
        return conn

    def _get_thread_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                _ = conn.total_changes
            except sqlite3.Error:
                self._drop_connection(conn)
                conn = None
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _drop_connection(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
        with self._connections_lock:
            self._connections.discard(conn)
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        with self._connections_lock:
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
        if getattr(self._local, "conn", None) in connections:
            self._local.conn = None
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def _connection(self) -> Iterator["_PooledConnection"]:
        conn = _PooledConnection(self, self._get_thread_connection())
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                self._drop_connection(conn.raw_connection)
            raise
        else:
            try:
                conn.commit()
            except sqlite3.Error:
                self._drop_connection(conn.raw_connection)
                raise


class _PooledConnection:
    def __init__(self, store: SqliteJobStore, conn: sqlite3.Connection) -> None:
        self._store = store
        self.raw_connection = conn

    def _reconnect(self) -> None:
        self._store._drop_connection(self.raw_connection)
        self.raw_connection = self._store._get_thread_connection()

    @staticmethod
    def _is_begin(sql: object) -> bool:
        return isinstance(sql, str) and sql.lstrip().upper().startswith("BEGIN")

    @staticmethod
    def _is_closed_connection_operational_error(exc: sqlite3.OperationalError) -> bool:
        msg = str(exc).lower()
        return (
            "closed database" in msg
            or "closed connection" in msg
            or "connection is closed" in msg
            or "bad parameter or other api misuse" in msg
        )

    def execute(self, sql: str, parameters=(), /):
        try:
            return self.raw_connection.execute(sql, parameters)
        except sqlite3.ProgrammingError:
            if not self._is_begin(sql):
                raise
            self._reconnect()
            return self.raw_connection.execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if not self._is_begin(sql) or not self._is_closed_connection_operational_error(exc):
                raise
            self._reconnect()
            return self.raw_connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters, /):
        return self.raw_connection.executemany(sql, parameters)

    def commit(self) -> None:
        self.raw_connection.commit()

    def rollback(self) -> None:
        self.raw_connection.rollback()

    def __getattr__(self, name: str):
        return getattr(self.raw_connection, name)


def _row_to_record(
    row: sqlite3.Row,
) -> JobRecord:
    invalid_reasons = _invalid_persisted_job_reasons(row)
    if invalid_reasons:
        raise ValueError("invalid persisted job row")
    return JobRecord(
        job_id=str(row["job_id"]),
        thread_dir=str(row["thread_dir"]),
        gt_file=Path(str(row["gt_file"])),
        proc_file=Path(str(row["proc_file"])),
        answer_position=str(row["answer_position"]),
        kind=JobKind(str(row["kind"])),
        status=JobStatus(str(row["status"])),
        created_at_s=float(row["created_at_s"]),
        started_at_s=float(row["started_at_s"]) if row["started_at_s"] is not None else None,
        finished_at_s=float(row["finished_at_s"]) if row["finished_at_s"] is not None else None,
        reward=float(row["reward"]) if row["reward"] is not None else None,
        msg=str(row["msg"] or ""),
    )


def _invalid_persisted_job_reasons(row: sqlite3.Row) -> list[str]:
    invalid_reasons: list[str] = []
    try:
        kind = JobKind(str(row["kind"]))
    except ValueError:
        kind = JobKind.REWARD
        invalid_reasons.append("kind=invalid")
    try:
        status = JobStatus(str(row["status"]))
    except ValueError:
        status = JobStatus.ERROR
        invalid_reasons.append("status=invalid")

    for field, required in (
        ("created_at_s", True),
        ("started_at_s", False),
        ("finished_at_s", False),
        ("reward", False),
    ):
        if not _row_numeric_value_is_valid(row[field], required=required):
            invalid_reasons.append(f"{field}=invalid")

    if kind is JobKind.REWARD and status is JobStatus.DONE and row["reward"] is None:
        invalid_reasons.append("reward=invalid")
    if kind is JobKind.REWARD and not _required_persisted_text_is_valid(row["answer_position"]):
        invalid_reasons.append("answer_position=invalid")
    if status is JobStatus.QUEUED:
        if row["started_at_s"] is not None and "started_at_s=invalid" not in invalid_reasons:
            invalid_reasons.append("started_at_s=invalid")
        if row["finished_at_s"] is not None and "finished_at_s=invalid" not in invalid_reasons:
            invalid_reasons.append("finished_at_s=invalid")
    elif status is JobStatus.RUNNING:
        if row["started_at_s"] is None and "started_at_s=invalid" not in invalid_reasons:
            invalid_reasons.append("started_at_s=invalid")
        if row["finished_at_s"] is not None and "finished_at_s=invalid" not in invalid_reasons:
            invalid_reasons.append("finished_at_s=invalid")
        if not _required_persisted_text_is_valid(row["worker_id"]):
            invalid_reasons.append("worker_id=invalid")
    elif status in {JobStatus.DONE, JobStatus.ERROR}:
        if row["finished_at_s"] is None and "finished_at_s=invalid" not in invalid_reasons:
            invalid_reasons.append("finished_at_s=invalid")
    for field in ("thread_dir", "gt_file", "proc_file"):
        if not _required_persisted_text_is_valid(row[field]):
            invalid_reasons.append(f"{field}=invalid")
    if not _required_persisted_text_is_valid(row["job_id"]):
        invalid_reasons.append("job_id=invalid")
    return invalid_reasons


def _required_persisted_text_is_valid(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _is_already_quarantined_invalid_row(row: sqlite3.Row) -> bool:
    if str(row["status"] or "") != JobStatus.ERROR.value:
        return False
    if not str(row["msg"] or "").startswith("invalid persisted job row:"):
        return False
    if not _row_numeric_value_is_valid(row["created_at_s"], required=True):
        return False
    if not _row_numeric_value_is_valid(row["finished_at_s"], required=True):
        return False
    if not _row_numeric_value_is_valid(row["reward"], required=True):
        return False
    return True


def _cleanup_retention_s(row: sqlite3.Row) -> float:
    created_at_s = _finite_row_float(row, "created_at_s", [], default=0.0)
    finished_at_s = _finite_row_float(row, "finished_at_s", [], default=created_at_s)
    return max(created_at_s, finished_at_s)


def _row_numeric_value_is_valid(value: object, *, required: bool) -> bool:
    if value is None:
        return not required
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    numeric = float(value)
    return math.isfinite(numeric) and abs(numeric) <= _MAX_PERSISTED_NUMERIC_ABS


def _cleanup_attempts_value_is_valid(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return 0 <= value <= _MAX_SQLITE_INTEGER
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > _MAX_SQLITE_INTEGER:
        return False
    attempts = int(value)
    return attempts <= _MAX_SQLITE_INTEGER and float(attempts) == float(value)


def _row_to_snapshot(row: sqlite3.Row) -> JobSnapshot:
    invalid_reasons = _invalid_persisted_job_reasons(row)
    try:
        kind = JobKind(str(row["kind"]))
    except ValueError:
        kind = JobKind.REWARD
    try:
        status = JobStatus(str(row["status"]))
    except ValueError:
        status = JobStatus.ERROR
    coercion_reasons: list[str] = []
    msg = str(row["msg"] or "")
    created_at_s = _finite_row_float(row, "created_at_s", coercion_reasons, default=0.0)
    started_at_s = _optional_finite_row_float(row, "started_at_s", coercion_reasons)
    finished_at_s = _optional_finite_row_float(row, "finished_at_s", coercion_reasons)
    reward = _optional_finite_row_float(row, "reward", coercion_reasons)
    if invalid_reasons:
        status = JobStatus.ERROR
        reward = 0.0
        msg = f"invalid persisted job row: {', '.join(invalid_reasons)}"
    return JobSnapshot(
        job_id=str(row["job_id"]),
        proc_file=Path(str(row["proc_file"])),
        kind=kind,
        status=status,
        created_at_s=created_at_s,
        started_at_s=started_at_s,
        finished_at_s=finished_at_s,
        reward=reward,
        msg=msg,
        gt_file=Path(str(row["gt_file"])) if "gt_file" in row.keys() else None,
        quarantined_invalid=bool(invalid_reasons),
    )


def _finite_row_float(
    row: sqlite3.Row,
    field: str,
    invalid_reasons: list[str],
    *,
    default: float,
) -> float:
    value = row[field]
    try:
        result = float(value)
    except (TypeError, ValueError):
        invalid_reasons.append(f"{field}=invalid")
        return default
    if not math.isfinite(result) or abs(result) > _MAX_PERSISTED_NUMERIC_ABS:
        invalid_reasons.append(f"{field}=invalid")
        return default
    return result


def _optional_finite_row_float(
    row: sqlite3.Row,
    field: str,
    invalid_reasons: list[str],
) -> float | None:
    if row[field] is None:
        return None
    return _finite_row_float(row, field, invalid_reasons, default=0.0)
