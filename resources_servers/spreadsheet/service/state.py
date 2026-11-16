"""Durable sessions and operation receipts for a single service replica.

Use a local disk for SQLite and locks. Scale by adding independent service
replicas with session affinity, not by sharing this database over NFS.
"""

import fcntl
import json
import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from resources_servers.spreadsheet.schemas import READ_ONLY, RECALCULATE_ON_WRITE, TOOL_MODELS

from . import workbook
from .catalog import sha256_file
from .errors import ConflictError, EnvironmentError, ToolError


class Store:
    def __init__(self, root: Path, catalog, engine, python_runner, workers: int = 8, max_pending: int = 256):
        self.root, self.catalog, self.engine, self.python_runner = root.resolve(), catalog, engine, python_runner
        self.max_pending = max_pending
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.root / "state.sqlite3"
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="spreadsheet-job")
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, rollout_id TEXT UNIQUE NOT NULL, task_id TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, created REAL NOT NULL,
                    UNIQUE(session_id, operation_id));
                CREATE TABLE IF NOT EXISTS executions (
                    session_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def workspace(self, session_id: str) -> Path:
        if len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id):
            raise ConflictError("Invalid session ID")
        return self.root / session_id

    @contextmanager
    def lock(self, session_id):
        root = self.workspace(session_id)
        root.mkdir(exist_ok=True, mode=0o700)
        path = root / ".lock"
        # Lock files are outside the Python-visible staging directory.
        with path.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def seed(self, rollout_id: str, task_id: str):
        if not rollout_id or len(rollout_id) > 128:
            raise ValueError("A bounded rollout_id is required")
        task = self.catalog.get(task_id)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM sessions WHERE rollout_id=?", (rollout_id,)).fetchone()
            if old:
                if old["task_id"] != task_id or old["state"] != "OPEN":
                    raise ConflictError("Rollout ID is already bound or closed")
                if db.execute("SELECT 1 FROM executions WHERE session_id=?", (old["id"],)).fetchone():
                    raise ConflictError("Rollout execution already started; do not restart inference")
                return {"session_id": old["id"], "task_id": task_id}
            session_id, now = uuid4().hex, time.time()
            root = self.workspace(session_id)
            root.mkdir(mode=0o700)
            try:
                if sha256_file(task.seed) != task.seed_sha256:
                    raise EnvironmentError("Seed changed since catalog validation")
                shutil.copyfile(task.seed, root / "data.xlsx")
                db.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?)", (session_id, rollout_id, task_id, "OPEN", now, now)
                )
            except Exception:
                shutil.rmtree(root)
                raise
            return {"session_id": session_id, "task_id": task_id}

    def session(self, session_id):
        self.workspace(session_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ConflictError("Unknown session")
        return dict(row)

    def claim_execution(self, session_id, execution_id):
        self.session(session_id)
        if not execution_id or len(execution_id) > 128:
            raise ValueError("Invalid execution ID")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT execution_id FROM executions WHERE session_id=?", (session_id,)).fetchone()
            if old and old[0] != execution_id:
                raise ConflictError("Session already has an agent execution; do not replay inference")
            db.execute("INSERT OR IGNORE INTO executions VALUES (?,?)", (session_id, execution_id))
        return {"claimed": True}

    def submit(self, session_id: str, operation_id: str, action: str, arguments: dict):
        self.session(session_id)
        if not operation_id or len(operation_id) > 256:
            raise ValueError("A bounded operation_id is required")
        if action not in TOOL_MODELS and action != "verify":
            raise ToolError("Unknown tool")
        payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload) > 128 * 1024:
            raise ToolError("Tool arguments exceed the size limit")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM jobs WHERE session_id=? AND operation_id=?", (session_id, operation_id)
            ).fetchone()
            if old:
                if old["payload"] != payload or old["action"] != action:
                    raise ConflictError("Operation ID was reused with different input")
                return self._public_job(old)
            current = db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
            if current is None or current["state"] != "OPEN":
                raise ConflictError("Session is frozen, closed, or indeterminate")
            pending = db.execute("SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
            if pending >= self.max_pending:
                raise EnvironmentError("Service queue is full; retry with backoff")
            job_id = uuid4().hex
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                (job_id, session_id, operation_id, action, payload, "QUEUED", None, time.time()),
            )
            db.execute("UPDATE sessions SET updated=? WHERE id=?", (time.time(), session_id))
        self.executor.submit(self._run, job_id)
        return self.job(job_id)

    @staticmethod
    def _public_job(row):
        result = {"job_id": row["id"], "status": row["status"]}
        if row["result"]:
            result["result"] = json.loads(row["result"])
        return result

    def job(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ConflictError("Unknown job")
        return self._public_job(row)

    def _run(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        with self.lock(row["session_id"]):
            with self.db() as db:
                changed = db.execute("UPDATE jobs SET status='RUNNING' WHERE id=? AND status='QUEUED'", (job_id,))
                if not changed.rowcount:
                    return
            try:
                session = self.session(row["session_id"])
                if session["state"] != "OPEN":
                    raise ToolError("Session is no longer open")
                result = self._execute(session, row["action"], json.loads(row["payload"]), job_id)
            except (ToolError, ValidationError) as exc:
                result = {"ok": False, "error_type": "tool_error", "error": str(exc)[:1000]}
            except Exception:
                result = {
                    "ok": False,
                    "error_type": "environment_error",
                    "error": "Execution backend failed; this is not an incorrect-answer reward",
                }
            with self.db() as db:
                db.execute("UPDATE jobs SET status='DONE', result=? WHERE id=?", (json.dumps(result), job_id))
                db.execute("UPDATE sessions SET updated=? WHERE id=?", (time.time(), row["session_id"]))

    def _execute(self, session, action, arguments, job_id):
        task = self.catalog.get(session["task_id"])
        root = self.workspace(session["id"])
        if action != "verify":
            model = TOOL_MODELS[action].model_validate(arguments)
            arguments = model.model_dump(by_alias=True)
        elif arguments:
            raise ToolError("Verifier arguments are server-owned")
        current = root / "data.xlsx"
        before_hash = sha256_file(current)
        if action in READ_ONLY:
            output = workbook.execute(current, action, arguments)
            return {"ok": True, "output": output, "workbook_sha256": before_hash}
        stage = root / ("stage_" + job_id)
        stage.mkdir(mode=0o700)
        path = stage / "data.xlsx"
        shutil.copyfile(current, path)
        try:
            if action == "code_interpreter":
                output = self.python_runner.execute(stage, arguments["code"])
                workbook.check_archive(path)
                # A successful Python exit does not establish a usable workbook.
                # Invalid model-created files are tool errors, not engine outages.
                try:
                    validated = workbook.load(path)
                    validated.close()
                except ImportError:
                    raise EnvironmentError("Workbook runtime dependency is unavailable") from None
                except Exception:
                    raise ToolError("Python produced an unreadable workbook; changes discarded") from None
            elif action == "verify":
                self.engine.recalculate(path)
                if sha256_file(task.oracle) != task.oracle_sha256:
                    raise EnvironmentError("Oracle changed since catalog validation")
                output = workbook.compare(task.oracle, path, task.answer_position)
            else:
                if action == "recalculate_and_read":
                    self.engine.recalculate(path)
                output = workbook.execute(path, action, arguments)
                if action in RECALCULATE_ON_WRITE:
                    self.engine.recalculate(path)
            workbook.check_archive(path)
            # A Python program can replace data.xlsx with a symlink; validate the
            # archive and filesystem target before the trusted commit.
            if path.is_symlink() or path.stat().st_nlink != 1:
                raise ToolError("Workbook must be a regular independent file")
            after_hash = sha256_file(path)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(path, current)
            if action == "verify":
                with self.db() as db:
                    db.execute("UPDATE sessions SET state='VERIFIED' WHERE id=?", (session["id"],))
            return {
                "ok": True,
                "output": output,
                "before_sha256": before_hash,
                "workbook_sha256": after_hash,
                "engine": self.engine.version,
                "dataset_revision": task.dataset_revision,
                "manifest_sha256": self.catalog.manifest_sha256,
            }
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def recover(self):
        """Call once at startup with one API process owning this store."""
        with self.db() as db:
            db.execute(
                "UPDATE sessions SET state='INDETERMINATE' WHERE id IN "
                "(SELECT session_id FROM jobs WHERE status='RUNNING')"
            )
            error = json.dumps(
                {
                    "ok": False,
                    "error_type": "environment_error",
                    "error": "Interrupted operation; session cannot be replayed",
                }
            )
            db.execute("UPDATE jobs SET status='DONE',result=? WHERE status='RUNNING'", (error,))
            queued = db.execute("SELECT id FROM jobs WHERE status='QUEUED'").fetchall()
        for row in queued:
            self.executor.submit(self._run, row["id"])

    def close_session(self, session_id):
        self.session(session_id)
        with self.lock(session_id):
            with self.db() as db:
                pending = db.execute(
                    "SELECT count(*) FROM jobs WHERE session_id=? AND status IN ('QUEUED','RUNNING')", (session_id,)
                ).fetchone()[0]
                if pending:
                    raise ConflictError("Session still has active jobs")
                db.execute("UPDATE sessions SET state='CLOSED', updated=? WHERE id=?", (time.time(), session_id))
        return {"closed": True}

    def cleanup(self, ttl_seconds: int):
        with self.db() as db:
            expired = db.execute("SELECT id FROM sessions WHERE updated < ?", (time.time() - ttl_seconds,)).fetchall()
        for row in expired:
            session_id = row["id"]
            with self.lock(session_id):
                with self.db() as db:
                    active = db.execute(
                        "SELECT count(*) FROM jobs WHERE session_id=? AND status IN ('QUEUED','RUNNING')",
                        (session_id,),
                    ).fetchone()[0]
                    updated = db.execute("SELECT updated FROM sessions WHERE id=?", (session_id,)).fetchone()
                    if active or updated is None or updated[0] >= time.time() - ttl_seconds:
                        continue
                    db.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
                    db.execute("DELETE FROM executions WHERE session_id=?", (session_id,))
                    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                    shutil.rmtree(self.workspace(session_id))

    def shutdown(self):
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.engine.close()
