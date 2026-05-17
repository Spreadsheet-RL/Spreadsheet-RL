from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import JobKind, JobRecord, JobStatus


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


class SqliteJobStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
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
                raise RuntimeError(
                    f"Database schema is missing the 'kind' column: {self._db_path}. "
                    "Delete the DB file or start with a fresh environment."
                )
            if "cleanup_next_retry_s" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN cleanup_next_retry_s REAL;")
            if "cleanup_attempts" not in columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN cleanup_attempts INTEGER NOT NULL DEFAULT 0;"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at_s);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_started ON jobs(status, started_at_s);"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_finished ON jobs(finished_at_s);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_finished_job_id ON jobs(finished_at_s, job_id);")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_cleanup_retry
                ON jobs(cleanup_next_retry_s, finished_at_s, job_id)
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
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_insert_queued
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
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_insert_running
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
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_insert_total
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
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_update_queued
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
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_update_running
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
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_delete_queued
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
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_delete_running
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
                CREATE TRIGGER IF NOT EXISTS trg_jobs_counter_delete_total
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
    ) -> bool:
        lease_name = name.strip()
        owner = owner_id.strip()
        if not lease_name:
            raise ValueError("lease name cannot be empty")
        if not owner:
            raise ValueError("owner_id cannot be empty")
        now = time.time() if now_s is None else float(now_s)
        lease_until = now + max(1.0, float(lease_s))
        with self._connect() as conn:
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

            current_owner = str(row["owner_id"] or "")
            current_until = float(row["lease_until_s"] or 0.0)
            if current_owner == owner or current_until <= now:
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
            return int(row[0])
        fallback = conn.execute(fallback_sql, fallback_params).fetchone()[0]
        return int(fallback)

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
        with self._connect() as conn:
            return self._get_queued_count(conn) < max(1, int(max_queue_size))

    def get_snapshot(self, job_id: str) -> JobSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    job_id,
                    proc_file,
                    kind,
                    status,
                    created_at_s,
                    started_at_s,
                    finished_at_s,
                    reward,
                    msg
                FROM jobs
                WHERE job_id = ?;
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_snapshot(row)

    def enqueue(self, job: JobRecord, *, max_queue_size: int) -> bool:
        with self._connect() as conn:
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
            WHERE job_id = (
                SELECT job_id
                FROM jobs
                WHERE status = ?
                ORDER BY created_at_s
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
            SELECT job_id FROM jobs
            WHERE status = ?
            ORDER BY created_at_s
            LIMIT 1;
            """,
            (JobStatus.QUEUED.value,),
        ).fetchone()
        if row is None:
            return None

        job_id = str(row["job_id"])
        updated = conn.execute(
            """
            UPDATE jobs
            SET status = ?, started_at_s = ?, worker_id = ?, updated_at_s = ?
            WHERE job_id = ? AND status = ?;
            """,
            (
                JobStatus.RUNNING.value,
                now,
                worker_id,
                now,
                job_id,
                JobStatus.QUEUED.value,
            ),
        ).rowcount
        if updated != 1:
            return None

        return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def claim_next(
        self,
        *,
        worker_id: str,
        max_running_jobs: int | None,
    ) -> JobRecord | None:
        with self._connect() as conn:
            now = time.time()
            has_explicit_tx = False

            if max_running_jobs is not None:
                # Serialize cross-process job claiming only when we need the
                # running-cap check + queued->running claim to be atomic.
                conn.execute("BEGIN IMMEDIATE")
                has_explicit_tx = True
                if self._get_running_count(conn) >= max_running_jobs:
                    conn.rollback()
                    return None

            try:
                row = self._claim_next_returning(conn, now=now, worker_id=worker_id)
            except sqlite3.OperationalError as exc:
                if "RETURNING" not in str(exc).upper():
                    raise
                if not has_explicit_tx:
                    conn.execute("BEGIN IMMEDIATE")
                    has_explicit_tx = True
                row = self._claim_next_legacy(conn, now=now, worker_id=worker_id)

            if row is None:
                if has_explicit_tx:
                    conn.rollback()
                return None

            conn.commit()
            return _row_to_record(row)

    def finish(
        self,
        *,
        job_id: str,
        status: JobStatus,
        reward: float,
        msg: str,
    ) -> None:
        with self._connect() as conn:
            now = time.time()
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, reward = ?, msg = ?, finished_at_s = ?, updated_at_s = ?
                WHERE job_id = ?;
                """,
                (status.value, float(reward), msg or "", now, now, job_id),
            )
            conn.commit()

    def mark_stale_running_as_error(self, *, older_than_s: float, msg: str) -> int:
        with self._connect() as conn:
            now = time.time()
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = ?, reward = 0.0, msg = ?, finished_at_s = ?, updated_at_s = ?
                WHERE status = ? AND started_at_s IS NOT NULL AND started_at_s < ?;
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
        mode: str = "all",
    ) -> list[tuple[str, JobKind, Path]]:
        mode_norm = mode.strip().lower()
        if mode_norm not in {"all", "fresh", "retry"}:
            raise ValueError(f"invalid cleanup list mode: {mode}")
        conditions = [
            "finished_at_s IS NOT NULL",
            "finished_at_s < ?",
        ]
        params: list[object] = [float(cutoff_s)]
        if mode_norm == "all":
            conditions.append("(cleanup_next_retry_s IS NULL OR cleanup_next_retry_s <= ?)")
            params.append(now_s)
        elif mode_norm == "fresh":
            conditions.append("cleanup_next_retry_s IS NULL")
        else:
            conditions.append("cleanup_next_retry_s IS NOT NULL")
            conditions.append("cleanup_next_retry_s <= ?")
            params.append(now_s)

        query = (
            "SELECT job_id, kind, proc_file FROM jobs "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY "
            + (
                "cleanup_next_retry_s, finished_at_s, job_id"
                if mode_norm == "retry"
                else "finished_at_s, job_id"
            )
        )
        if limit is not None:
            n = max(1, int(limit))
            query += " LIMIT ?"
            params.append(n)
        query += ";"

        rows = conn.execute(query, tuple(params)).fetchall()
        result: list[tuple[str, JobKind, Path]] = []
        for row in rows:
            proc_file = row["proc_file"]
            if not proc_file:
                continue
            kind_raw = str(row["kind"] or JobKind.REWARD.value)
            try:
                kind = JobKind(kind_raw)
            except ValueError:
                kind = JobKind.REWARD
            result.append((str(row["job_id"]), kind, Path(str(proc_file))))
        return result

    def list_cleanup_batch(
        self,
        *,
        cutoff_s: float,
        batch_size: int,
        retry_batch_share: float,
        now_s: float | None = None,
    ) -> list[tuple[str, JobKind, Path]]:
        size = max(1, int(batch_size))
        retry_share = min(max(0.0, float(retry_batch_share)), 1.0)
        retry_target = int(size * retry_share)
        if retry_share > 0.0:
            retry_target = max(1, retry_target)
        retry_target = min(size, retry_target)
        fresh_target = max(0, size - retry_target)

        with self._connect() as conn:
            now = time.time() if now_s is None else float(now_s)
            jobs: list[tuple[str, JobKind, Path]] = []
            seen_job_ids: set[str] = set()

            def _append_unique(candidates: list[tuple[str, JobKind, Path]]) -> None:
                for candidate in candidates:
                    if candidate[0] in seen_job_ids:
                        continue
                    seen_job_ids.add(candidate[0])
                    jobs.append(candidate)
                    if len(jobs) >= size:
                        break

            retry_jobs: list[tuple[str, JobKind, Path]] = []
            fresh_jobs: list[tuple[str, JobKind, Path]] = []
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
        with self._connect() as conn:
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
                    attempts = int(row["cleanup_attempts"])
                    retry_delay_s = min(max_retry_s, base_retry_s * (2 ** min(attempts, 10)))
                    updates.append((attempts + 1, now_s + retry_delay_s, str(row["job_id"])))

                conn.executemany(
                    """
                    UPDATE jobs
                    SET cleanup_attempts = ?,
                        cleanup_next_retry_s = ?
                    WHERE job_id = ? AND finished_at_s IS NOT NULL;
                    """,
                    updates,
                )
                total_updates += len(updates)
            conn.commit()
            return total_updates

    def delete_jobs(self, *, job_ids: list[str]) -> int:
        if not job_ids:
            return 0
        with self._connect() as conn:
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

    def delete_finished_before(self, *, cutoff_s: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM jobs
                WHERE finished_at_s IS NOT NULL AND finished_at_s < ?;
                """,
                (float(cutoff_s),),
            )
            conn.commit()
            return int(cur.rowcount)

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            queued = self._get_queued_count(conn)
            running = self._get_running_count(conn)
            total = self._get_total_count(conn)
        return {
            "queued": int(queued),
            "running": int(running),
            "jobs": int(total),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30,  # Sets the per-connection busy timeout (avoids "database is locked").
            # Each store operation opens a fresh SQLite connection, which is only used
            # within the current thread (via `asyncio.to_thread`), so this is safe.
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # `synchronous` is a per-connection setting; apply it on every connection.
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn


def _row_to_record(
    row: sqlite3.Row,
    *,
    status: JobStatus | None = None,
    started_at_s: float | None = None,
    worker_id: str | None = None,  # only for debug; not stored on record currently
    updated_at_s: float | None = None,  # noqa: ARG001 - keep parity with claim_next return
) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        thread_dir=str(row["thread_dir"]),
        gt_file=Path(str(row["gt_file"])),
        proc_file=Path(str(row["proc_file"])),
        answer_position=str(row["answer_position"]),
        kind=JobKind(str(row["kind"])),
        status=status or JobStatus(str(row["status"])),
        created_at_s=float(row["created_at_s"]),
        started_at_s=started_at_s if started_at_s is not None else (float(row["started_at_s"]) if row["started_at_s"] is not None else None),
        finished_at_s=float(row["finished_at_s"]) if row["finished_at_s"] is not None else None,
        reward=float(row["reward"]) if row["reward"] is not None else None,
        msg=str(row["msg"] or ""),
    )


def _row_to_snapshot(row: sqlite3.Row) -> JobSnapshot:
    return JobSnapshot(
        job_id=str(row["job_id"]),
        proc_file=Path(str(row["proc_file"])),
        kind=JobKind(str(row["kind"])),
        status=JobStatus(str(row["status"])),
        created_at_s=float(row["created_at_s"]),
        started_at_s=float(row["started_at_s"]) if row["started_at_s"] is not None else None,
        finished_at_s=float(row["finished_at_s"]) if row["finished_at_s"] is not None else None,
        reward=float(row["reward"]) if row["reward"] is not None else None,
        msg=str(row["msg"] or ""),
    )
