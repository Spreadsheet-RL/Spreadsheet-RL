from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .excel_pool import ExcelWorkerPool, ExcelWorkerProcess, _process_creation_time as _windows_process_creation_time
from .job_store import JobSnapshot, SqliteJobStore
from .models import JobKind, JobRecord, JobStatus
from .platform import Platform, detect_platform


def _get_output_root() -> Path:
    value = os.environ.get("REWARD_API_OUTPUT_ROOT")
    if value:
        value = os.path.expandvars(value)
        return Path(value).expanduser()
    return Path.home() / "async_reward_api_output"


def _get_db_path() -> Path:
    value = os.environ.get("REWARD_API_DB_PATH")
    if value:
        value = os.path.expandvars(value)
        return Path(value).expanduser()
    return Path.home() / ".async_reward_api" / "jobs.sqlite3"


def _get_platform() -> Platform:
    return detect_platform()


def _fingerprint_path(path: Path) -> str:
    try:
        text = str(path.resolve(strict=False))
    except OSError:
        text = str(path)
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


def _get_windows_excel_diagnostics_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Temp" / "Diagnostics"
    return Path.home() / "AppData" / "Local" / "Temp" / "Diagnostics"


def _get_windows_excel_diagnostics_dir() -> Path:
    default = _get_windows_excel_diagnostics_root() / "EXCEL"
    value = os.environ.get("REWARD_API_WINDOWS_EXCEL_DIAGNOSTICS_DIR")
    if value is None or not value.strip():
        return default
    value = os.path.expandvars(value)
    return Path(value).expanduser()


def _is_safe_windows_excel_diagnostics_dir(path: Path) -> bool:
    try:
        diagnostics_root = _get_windows_excel_diagnostics_root().resolve(strict=False)
        target = path.resolve(strict=False)
    except OSError:
        return False
    return target != diagnostics_root and target.is_relative_to(diagnostics_root)


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


def _get_worker_timeout_s() -> float:
    value = os.environ.get("REWARD_API_WORKER_TIMEOUT_S", "240")
    try:
        timeout_s = float(value)
        if timeout_s <= 0:
            return 240.0
        return timeout_s
    except ValueError:
        return 240.0


def _get_instance_per_worker() -> int:
    if os.name != "nt":
        return 0
    value = os.environ.get("REWARD_API_INSTANCE_PER_WORKER")
    if value is None or not value.strip():
        return 1
    try:
        n = int(value)
        return max(0, n)
    except ValueError:
        return 1


def _get_job_ttl_s() -> float:
    value = os.environ.get("REWARD_API_JOB_TTL_S", "3600")
    try:
        ttl_s = float(value)
        return max(60.0, ttl_s)
    except ValueError:
        return 3600.0


def _get_max_queue_size() -> int:
    value = os.environ.get("REWARD_API_MAX_QUEUE_SIZE", "3000")
    try:
        n = int(value)
        return max(1, n)
    except ValueError:
        return 3000


def _get_cleanup_batch_size() -> int:
    value = os.environ.get("REWARD_API_CLEANUP_BATCH_SIZE", "512")
    try:
        n = int(value)
        return min(max(1, n), 10000)
    except ValueError:
        return 512


def _get_cleanup_max_batches() -> int:
    value = os.environ.get("REWARD_API_CLEANUP_MAX_BATCHES", "8")
    try:
        n = int(value)
        return min(max(1, n), 1000)
    except ValueError:
        return 8


def _get_cleanup_leader_lease_s() -> float:
    value = os.environ.get("REWARD_API_CLEANUP_LEADER_LEASE_S", "900")
    try:
        n = float(value)
        return min(max(30.0, n), 3600.0)
    except ValueError:
        return 900.0


def _get_stale_sweep_leader_lease_s() -> float:
    value = os.environ.get("REWARD_API_STALE_SWEEP_LEADER_LEASE_S", "30")
    try:
        n = float(value)
        return min(max(5.0, n), 300.0)
    except ValueError:
        return 30.0


def _get_cleanup_retry_after_s() -> float:
    value = os.environ.get("REWARD_API_CLEANUP_RETRY_AFTER_S", "300")
    try:
        n = float(value)
        return min(max(1.0, n), 3600.0)
    except ValueError:
        return 300.0


def _get_cleanup_retry_max_s() -> float:
    value = os.environ.get("REWARD_API_CLEANUP_RETRY_MAX_S", "3600")
    try:
        n = float(value)
        return min(max(60.0, n), 86400.0)
    except ValueError:
        return 3600.0


def _get_cleanup_retry_batch_share() -> float:
    value = os.environ.get("REWARD_API_CLEANUP_RETRY_BATCH_SHARE", "0.25")
    try:
        n = float(value)
        return min(max(0.0, n), 1.0)
    except ValueError:
        return 0.25


def _get_idle_poll_max_s(base_poll_s: float) -> float:
    value = os.environ.get("REWARD_API_IDLE_POLL_MAX_S", "2.0")
    try:
        n = float(value)
        return min(max(float(base_poll_s), n), 30.0)
    except ValueError:
        return min(max(float(base_poll_s), 2.0), 30.0)


def _get_result_poll_interval_s() -> float:
    value = os.environ.get("REWARD_API_RESULT_POLL_INTERVAL_S")
    if value is None or not value.strip():
        return _get_poll_interval_s()
    try:
        poll_s = float(value)
        return min(max(0.05, poll_s), 5.0)
    except ValueError:
        return _get_poll_interval_s()


def _get_result_poll_max_s(base_poll_s: float) -> float:
    value = os.environ.get("REWARD_API_RESULT_POLL_MAX_S")
    if value is None or not value.strip():
        result_poll_value = os.environ.get("REWARD_API_RESULT_POLL_INTERVAL_S")
        legacy_poll_value = os.environ.get("REWARD_API_POLL_INTERVAL_S")
        if (result_poll_value is None or not result_poll_value.strip()) and (
            legacy_poll_value is not None and legacy_poll_value.strip()
        ):
            return float(base_poll_s)
        return min(max(float(base_poll_s), 1.0), 10.0)
    try:
        n = float(value)
        return min(max(float(base_poll_s), n), 10.0)
    except ValueError:
        return float(base_poll_s)


def _get_sample_meta_cache_size() -> int:
    value = os.environ.get("REWARD_API_SAMPLE_META_CACHE_SIZE", "2048")
    try:
        n = int(value)
        return min(max(0, n), 50000)
    except ValueError:
        return 2048


def _keep_files() -> bool:
    return os.environ.get("REWARD_API_KEEP_FILES", "0").strip() not in {"", "0", "false", "False"}


def _get_recalculate_job_root() -> Path:
    value = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
    if value:
        value = os.path.expandvars(value)
        return Path(value).expanduser()
    return Path(tempfile.gettempdir()) / "async_reward_api_recalculate_jobs"


def _format_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _worker_creationflags() -> int:
    flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return flags


def _enable_timeout_excel_fallback_kill() -> bool:
    value = os.environ.get("REWARD_API_TIMEOUT_EXCEL_FALLBACK_KILL", "0").strip()
    return value not in {"", "0", "false", "False"}


def _kill_subprocess_tree(proc: subprocess.Popen[bytes]) -> None:
    pid = int(proc.pid or 0)
    if pid <= 0:
        return

    if os.name == "nt":
        try:
            run_kwargs = {}
            flags = _worker_creationflags()
            if flags:
                run_kwargs["creationflags"] = flags
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                **run_kwargs,
            )
            if int(completed.returncode) == 0:
                return
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        return

    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_worker_subprocess(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
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

    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603,S607 - controlled local command
    try:
        stdout, stderr = proc.communicate(timeout=float(timeout_s))
        return False, stdout or b"", stderr or b""
    except subprocess.TimeoutExpired:
        _kill_subprocess_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = b"", b""
        return True, stdout or b"", stderr or b""


def _pid_is_excel(*, platform: Platform, pid: int) -> bool:
    if pid <= 0:
        return False

    if platform is Platform.WINDOWS and os.name == "nt":
        try:
            run_kwargs = {}
            flags = _worker_creationflags()
            if flags:
                run_kwargs["creationflags"] = flags
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                text=True,
                encoding="utf-8",
                errors="replace",
                **run_kwargs,
            )
            rows = [
                row.strip()
                for row in (completed.stdout or "").splitlines()
                if row.strip() and not row.strip().upper().startswith("INFO:")
            ]
            if not rows:
                return False
            row = next(csv.reader([rows[0]]))
            if len(row) < 2:
                return False
            try:
                listed_pid = int(row[1].replace(",", ""))
            except ValueError:
                return False
            return listed_pid == pid and row[0].strip().upper() == "EXCEL.EXE"
        except Exception:
            return False

    return False


def _kill_excel_pid(
    *,
    platform: Platform,
    pid: int,
    expected_creation_time: int | None = None,
) -> bool:
    if pid <= 0:
        return False

    if platform is Platform.WINDOWS and os.name == "nt":
        if expected_creation_time is None:
            return False
        current_creation_time = _windows_process_creation_time(pid)
        if current_creation_time is None or int(current_creation_time) != int(expected_creation_time):
            return False
        if not _pid_is_excel(platform=platform, pid=pid):
            return False
        try:
            run_kwargs = {}
            flags = _worker_creationflags()
            if flags:
                run_kwargs["creationflags"] = flags
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                **run_kwargs,
            )
            if int(completed.returncode) != 0:
                return False
            for _ in range(20):
                if not _pid_is_excel(platform=platform, pid=pid):
                    return True
                time.sleep(0.1)
            return not _pid_is_excel(platform=platform, pid=pid)
        except Exception:
            return False

    return False


def _kill_excel_pid_from_file(*, platform: Platform, pid_file: Path) -> bool:
    try:
        text = pid_file.read_text(encoding="utf-8").strip()
        expected_creation_time: int | None = None
        if text.startswith("{"):
            payload = json.loads(text)
            if not isinstance(payload, dict):
                return False
            pid = int(payload.get("pid") or 0)
            raw_creation_time = payload.get("creation_time")
            if raw_creation_time is None:
                return False
            expected_creation_time = int(raw_creation_time)
        else:
            return False
    except Exception:
        return False
    return _kill_excel_pid(
        platform=platform,
        pid=pid,
        expected_creation_time=expected_creation_time,
    )

def _list_excel_pids(platform: Platform) -> set[int]:
    if platform is Platform.WINDOWS and os.name == "nt":
        try:
            run_kwargs = {}
            flags = _worker_creationflags()
            if flags:
                run_kwargs["creationflags"] = flags
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                text=True,
                encoding="utf-8",
                errors="replace",
                **run_kwargs,
            )
            pids: set[int] = set()
            for line in (completed.stdout or "").splitlines():
                row_text = line.strip()
                if not row_text or row_text.upper().startswith("INFO:"):
                    continue
                try:
                    row = next(csv.reader([row_text]))
                except Exception:
                    continue
                if len(row) < 2:
                    continue
                try:
                    pids.add(int(row[1].replace(",", "")))
                except ValueError:
                    continue
            return pids
        except Exception:
            return set()

    return set()


def _kill_new_excel_processes(*, platform: Platform, baseline_pids: set[int]) -> int:
    current = _list_excel_pids(platform)
    to_kill = [pid for pid in current if pid not in baseline_pids]
    killed = 0
    for pid in to_kill:
        creation_time: int | None = None
        if platform is Platform.WINDOWS and os.name == "nt":
            creation_time = _windows_process_creation_time(pid)
        if _kill_excel_pid(
            platform=platform,
            pid=pid,
            expected_creation_time=creation_time,
        ):
            killed += 1
    return killed


def _resolve_path(path: Path) -> Path:
    return path.resolve()


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _list_attachment_names(path: Path) -> list[str]:
    attachments: list[str] = []
    for entry in path.iterdir():
        if entry.is_file() and entry.suffix.lower() in {".xlsx", ".csv"}:
            attachments.append(entry.name)
    return attachments


def _copy_upload_to_path(source, destination: Path) -> None:
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def _path_exists(path: Path) -> bool:
    return path.exists()


def _mkdir_parents(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _unlink_missing_ok(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _rmtree_with_retries(path: Path, delays_s: tuple[float, ...]) -> bool:
    for delay_s in delays_s:
        if delay_s:
            await asyncio.sleep(delay_s)
        try:
            await asyncio.to_thread(shutil.rmtree, path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            continue
    return False


async def _unlink_with_retries(path: Path, delays_s: tuple[float, ...]) -> bool:
    for delay_s in delays_s:
        if delay_s:
            await asyncio.sleep(delay_s)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class _RewardSampleMetadata:
    answer_position: str
    primary_ext: str
    gt_file: Path


_SAMPLE_META_CACHE_LOCK = threading.Lock()
_SAMPLE_META_CACHE: OrderedDict[tuple[str, int, int], _RewardSampleMetadata] = OrderedDict()


def _resolve_sample_metadata(sample_dir: Path) -> _RewardSampleMetadata:
    instruction_path = sample_dir / "instruction.json"
    try:
        instruction = _read_json(instruction_path)
        answer_position = str(instruction["answer_position"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Failed to read instruction.json: {exc}") from exc

    try:
        attachments = _list_attachment_names(sample_dir)
    except OSError as exc:
        raise RuntimeError(f"Failed to list attachments in {sample_dir}: {exc}") from exc
    if not attachments:
        raise ValueError(f"No .xlsx or .csv attachments found in {sample_dir}")

    ranked = sorted(attachments, key=lambda n: (0 if n.lower().endswith(".xlsx") else 1, n.lower()))
    primary_name = ranked[0]
    primary_ext = Path(primary_name).suffix.lower().lstrip(".")
    return _RewardSampleMetadata(
        answer_position=answer_position,
        primary_ext=primary_ext,
        gt_file=sample_dir / f"target.{primary_ext}",
    )


def _get_cached_sample_metadata(sample_dir: Path) -> _RewardSampleMetadata:
    cache_size = _get_sample_meta_cache_size()
    if cache_size <= 0:
        return _resolve_sample_metadata(sample_dir)

    try:
        sample_stat = sample_dir.stat()
        instruction_stat = (sample_dir / "instruction.json").stat()
        cache_key = (
            str(sample_dir.resolve()),
            int(sample_stat.st_mtime_ns),
            int(instruction_stat.st_mtime_ns),
        )
    except OSError as exc:
        raise RuntimeError(f"Failed to read sample metadata from {sample_dir}: {exc}") from exc

    with _SAMPLE_META_CACHE_LOCK:
        hit = _SAMPLE_META_CACHE.get(cache_key)
        if hit is not None:
            _SAMPLE_META_CACHE.move_to_end(cache_key)
            return hit

    metadata = _resolve_sample_metadata(sample_dir)

    with _SAMPLE_META_CACHE_LOCK:
        _SAMPLE_META_CACHE[cache_key] = metadata
        _SAMPLE_META_CACHE.move_to_end(cache_key)
        while len(_SAMPLE_META_CACHE) > cache_size:
            _SAMPLE_META_CACHE.popitem(last=False)
    return metadata


class RewardJobManager:
    def __init__(self, *, store: SqliteJobStore, platform: Platform) -> None:
        self._store = store
        self._platform = platform
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._instance_id = uuid.uuid4().hex
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
        self._max_running_jobs = max_running_jobs
        self._excel_pool: ExcelWorkerPool | None = None
        self._run_sem: asyncio.Semaphore | None = None
        self._job_tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._windows_excel_diagnostics_cleanup_task: asyncio.Task | None = None
        self._excel_pool_start_failed = False

    async def start(self) -> None:
        await asyncio.to_thread(self._store.init)
        if (
            self._platform is Platform.WINDOWS
            and os.name == "nt"
            and self._instance_per_worker > 0
        ):
            self._excel_pool = ExcelWorkerPool(
                size=self._instance_per_worker,
                platform=self._platform.value,
            )
            try:
                await self._excel_pool.start()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[main] Excel pool start failed; falling back to per-job workers: {_format_exception(exc)}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    await self._excel_pool.shutdown(force=True)
                except Exception:
                    pass
                self._excel_pool = None
                self._excel_pool_start_failed = True
                if self._max_running_jobs is None:
                    self._max_running_jobs = 1

        concurrency = self._excel_pool.size if self._excel_pool is not None else 1
        self._run_sem = asyncio.Semaphore(concurrency)
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self._platform is Platform.WINDOWS and os.name == "nt":
            self._windows_excel_diagnostics_cleanup_task = asyncio.create_task(
                self._windows_excel_diagnostics_cleanup_loop()
            )

    async def shutdown(self) -> None:
        self._stop.set()
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
        if self._job_tasks:
            tasks = list(self._job_tasks)
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self._excel_pool is not None:
            await self._excel_pool.shutdown(force=True)
            self._excel_pool = None

    async def submit(self, job: JobRecord) -> bool:
        return await asyncio.to_thread(self._store.enqueue, job, max_queue_size=_get_max_queue_size())

    async def has_queue_capacity(self) -> bool:
        return await asyncio.to_thread(
            self._store.has_queue_capacity,
            max_queue_size=_get_max_queue_size(),
        )

    async def get_snapshot(self, job_id: str) -> JobSnapshot | None:
        return await asyncio.to_thread(self._store.get_snapshot, job_id)

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

    async def _worker_loop(self) -> None:
        idle_sleep_s = self._poll_interval_s
        while not self._stop.is_set():
            if self._run_sem is None:
                raise RuntimeError("RewardJobManager is not started")
            await self._run_sem.acquire()

            excel_worker: ExcelWorkerProcess | None = None
            use_excel_pool = False
            try:
                if self._excel_pool is not None:
                    excel_worker = await self._excel_pool.acquire()
                    use_excel_pool = True

                job = await asyncio.to_thread(
                    self._store.claim_next,
                    worker_id=self._worker_id,
                    max_running_jobs=self._max_running_jobs,
                )
            except asyncio.CancelledError:
                if excel_worker is not None and self._excel_pool is not None:
                    await self._excel_pool.release(excel_worker)
                self._run_sem.release()
                raise
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[_worker_loop] job acquisition failed: {_format_exception(exc)}",
                    file=sys.stderr,
                    flush=True,
                )
                if excel_worker is not None and self._excel_pool is not None:
                    await self._excel_pool.release(excel_worker)
                self._run_sem.release()
                await asyncio.sleep(1.0)
                continue
            if job is None:
                if excel_worker is not None and self._excel_pool is not None:
                    await self._excel_pool.release(excel_worker)
                self._run_sem.release()
                await asyncio.sleep(idle_sleep_s)
                idle_sleep_s = min(idle_sleep_s * 1.5, self._idle_poll_max_s)
                continue

            idle_sleep_s = self._poll_interval_s
            task = asyncio.create_task(
                self._run_job(job, excel_worker=excel_worker, use_excel_pool=use_excel_pool)
            )
            self._job_tasks.add(task)
            task.add_done_callback(self._job_tasks.discard)

    async def _run_job(
        self,
        job: JobRecord,
        *,
        excel_worker: ExcelWorkerProcess | None,
        use_excel_pool: bool,
    ) -> None:
        try:
            if job.kind is JobKind.REWARD:
                reward, msg = await self._compute_reward(
                    job, excel_worker=excel_worker, use_excel_pool=use_excel_pool
                )
                await asyncio.to_thread(
                    self._store.finish,
                    job_id=job.job_id,
                    status=JobStatus.DONE,
                    reward=reward,
                    msg=msg,
                )
            elif job.kind is JobKind.RECALCULATE:
                msg = await self._recalc_job(job, excel_worker=excel_worker, use_excel_pool=use_excel_pool)
                await asyncio.to_thread(
                    self._store.finish,
                    job_id=job.job_id,
                    status=JobStatus.DONE,
                    reward=0.0,
                    msg=msg,
                )
            else:
                raise RuntimeError(f"unknown job kind: {job.kind}")
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self._store.finish,
                job_id=job.job_id,
                status=JobStatus.ERROR,
                reward=0.0,
                msg="job cancelled",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(
                self._store.finish,
                job_id=job.job_id,
                status=JobStatus.ERROR,
                reward=0.0,
                msg=f"worker exception: {_format_exception(exc)}",
            )
        finally:
            if self._run_sem is not None:
                self._run_sem.release()
            if job.kind is JobKind.REWARD and not _keep_files():
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

    async def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            try:
                pass_now_s = time.time()
                stale_lease_s = _get_stale_sweep_leader_lease_s()
                owns_stale_sweep_lease = await asyncio.to_thread(
                    self._store.try_acquire_maintenance_lease,
                    name="stale_sweep",
                    owner_id=self._worker_id,
                    lease_s=stale_lease_s,
                    now_s=pass_now_s,
                )
                if owns_stale_sweep_lease:
                    await asyncio.to_thread(
                        self._store.mark_stale_running_as_error,
                        older_than_s=_get_worker_timeout_s() + 60.0,
                        msg="stale running job (worker crashed?)",
                    )

                lease_s = _get_cleanup_leader_lease_s()
                owns_cleanup_lease = await asyncio.to_thread(
                    self._store.try_acquire_maintenance_lease,
                    name="cleanup",
                    owner_id=self._worker_id,
                    lease_s=lease_s,
                    now_s=pass_now_s,
                )
                if not owns_cleanup_lease:
                    continue

                ttl_s = _get_job_ttl_s()
                cutoff_s = pass_now_s - ttl_s
                if _keep_files():
                    await asyncio.to_thread(self._store.delete_finished_before, cutoff_s=cutoff_s)
                    continue
                recalc_root = _get_recalculate_job_root()
                try:
                    recalc_root_resolved = recalc_root.resolve()
                except OSError:
                    recalc_root_resolved = recalc_root

                batch_size = _get_cleanup_batch_size()
                retry_after_s = _get_cleanup_retry_after_s()
                retry_max_s = _get_cleanup_retry_max_s()
                retry_batch_share = _get_cleanup_retry_batch_share()
                delete_delays_s = (0.0, 0.25, 1.0) if os.name == "nt" else (0.0,)
                for _ in range(_get_cleanup_max_batches()):
                    still_leader = await asyncio.to_thread(
                        self._store.try_acquire_maintenance_lease,
                        name="cleanup",
                        owner_id=self._worker_id,
                        lease_s=lease_s,
                        now_s=time.time(),
                    )
                    if not still_leader:
                        break

                    jobs = await asyncio.to_thread(
                        self._store.list_cleanup_batch,
                        cutoff_s=cutoff_s,
                        batch_size=batch_size,
                        retry_batch_share=retry_batch_share,
                        now_s=time.time(),
                    )
                    if not jobs:
                        break

                    deletable_job_ids: list[str] = []
                    failed_job_ids: list[str] = []

                    for job_id, kind, proc_file in jobs:
                        parent = proc_file.parent
                        try:
                            parent_resolved = parent.resolve()
                        except OSError:
                            parent_resolved = parent
                        parent_is_recalc_job_dir = (
                            kind is JobKind.RECALCULATE
                            and parent_resolved.is_relative_to(recalc_root_resolved)
                            and parent_resolved != recalc_root_resolved
                        )
                        if parent_is_recalc_job_dir:
                            if not await _rmtree_with_retries(parent, delete_delays_s):
                                failed_job_ids.append(job_id)
                                continue
                            deletable_job_ids.append(job_id)
                            continue

                        if not await _unlink_with_retries(proc_file, delete_delays_s):
                            failed_job_ids.append(job_id)
                            continue

                        deletable_job_ids.append(job_id)

                    if deletable_job_ids:
                        await asyncio.to_thread(self._store.delete_jobs, job_ids=deletable_job_ids)
                    if failed_job_ids:
                        await asyncio.to_thread(
                            self._store.mark_cleanup_failed,
                            job_ids=failed_job_ids,
                            retry_after_s=retry_after_s,
                            retry_max_s=retry_max_s,
                        )
                    if len(jobs) < batch_size:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[_cleanup_loop] iteration failed: {_format_exception(exc)}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def _windows_excel_diagnostics_cleanup_loop(self) -> None:
        diagnostics_dir = _get_windows_excel_diagnostics_dir()
        if not _is_safe_windows_excel_diagnostics_dir(diagnostics_dir):
            raise RuntimeError("unsafe Windows Excel diagnostics cleanup directory")
        while not self._stop.is_set():
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
        if self._platform is Platform.WINDOWS and os.name == "nt":
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

    async def stats(self) -> dict[str, object]:
        counts = await asyncio.to_thread(self._store.stats)
        background_tasks, background_tasks_healthy = self._background_task_statuses()
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
            concurrency = 1
        else:
            excel_pool = self._excel_pool.status()
            excel_pool["startup_failed"] = False
            excel_pool_healthy = int(excel_pool.get("alive_instances") or 0) > 0
            concurrency = self._excel_pool.size
        ready = background_tasks_healthy and excel_pool_healthy
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
            "ready": ready,
        }


async def _recalc_file_via_worker(*, proc_file: Path, platform: Platform) -> tuple[bool, str]:
    excel_pid_file = Path(tempfile.gettempdir()) / f"async_reward_api_excel_pid_{uuid.uuid4().hex}.txt"
    use_fallback_excel_kill = _enable_timeout_excel_fallback_kill()
    baseline_excel_pids: set[int] = set()
    if use_fallback_excel_kill:
        baseline_excel_pids = await asyncio.to_thread(_list_excel_pids, platform)
    cmd = [
        sys.executable,
        "-m",
        "async_reward_api.worker",
        "--platform",
        platform.value,
        "--proc-file",
        str(proc_file),
        "--recalc-only",
        "--excel-pid-file",
        str(excel_pid_file),
    ]

    timeout_s = _get_worker_timeout_s()
    try:
        timed_out, stdout_bytes, stderr_bytes = await asyncio.to_thread(
            _run_worker_subprocess,
            cmd=cmd,
            timeout_s=timeout_s,
        )
        if timed_out:
            killed_specific = await asyncio.to_thread(
                _kill_excel_pid_from_file,
                platform=platform,
                pid_file=excel_pid_file,
            )
            killed_fallback = 0
            if not killed_specific and use_fallback_excel_kill:
                killed_fallback = await asyncio.to_thread(
                    _kill_new_excel_processes,
                    platform=platform,
                    baseline_pids=baseline_excel_pids,
                )
            if not killed_specific:
                print(
                    "[main] recalc worker timed out without attributed Excel cleanup "
                    f"(fallback_killed={killed_fallback})",
                    file=sys.stderr,
                    flush=True,
                )
            return False, f"timeout after {timeout_s:.0f}s"

        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()

        if not stdout_text:
            print(
                f"[_recalc_file_via_worker] empty worker response. stderr: {stderr_text}",
                file=sys.stderr,
                flush=True,
            )
            return False, "empty worker response"

        try:
            payload = json.loads(stdout_text)
            if not isinstance(payload, dict):
                print(
                    f"[_recalc_file_via_worker] invalid worker response format: {payload!r} "
                    f"stderr: {stderr_text}",
                    file=sys.stderr,
                    flush=True,
                )
                return False, "invalid worker response"
            ok = bool(payload.get("ok", False))
            msg = str(payload.get("msg", "") or "")
            if ok:
                return True, msg
            print(
                f"[_recalc_file_via_worker] worker reported failure: {msg} stderr: {stderr_text}",
                file=sys.stderr,
                flush=True,
            )
            return False, "recalc failed"
        except Exception as exc:
            print(
                f"[_recalc_file_via_worker] invalid worker response: {stdout_text!r} "
                f"stderr: {stderr_text}; error: {_format_exception(exc)}",
                file=sys.stderr,
                flush=True,
            )
            return False, "invalid worker response"
    finally:
        await asyncio.to_thread(_unlink_missing_ok, excel_pid_file)


async def _compute_reward_via_worker(
    *,
    gt_file: Path,
    proc_file: Path,
    answer_position: str,
    platform: Platform,
) -> tuple[float, str]:
    excel_pid_file = Path(tempfile.gettempdir()) / f"async_reward_api_excel_pid_{uuid.uuid4().hex}.txt"
    use_fallback_excel_kill = _enable_timeout_excel_fallback_kill()
    baseline_excel_pids: set[int] = set()
    if use_fallback_excel_kill:
        baseline_excel_pids = await asyncio.to_thread(_list_excel_pids, platform)
    cmd = [
        sys.executable,
        "-m",
        "async_reward_api.worker",
        "--platform",
        platform.value,
        "--gt-file",
        str(gt_file),
        "--proc-file",
        str(proc_file),
        "--answer-position",
        answer_position,
        "--excel-pid-file",
        str(excel_pid_file),
    ]

    timeout_s = _get_worker_timeout_s()
    try:
        timed_out, stdout_bytes, stderr_bytes = await asyncio.to_thread(
            _run_worker_subprocess,
            cmd=cmd,
            timeout_s=timeout_s,
        )
        if timed_out:
            killed_specific = await asyncio.to_thread(
                _kill_excel_pid_from_file,
                platform=platform,
                pid_file=excel_pid_file,
            )
            killed_fallback = 0
            if not killed_specific and use_fallback_excel_kill:
                killed_fallback = await asyncio.to_thread(
                    _kill_new_excel_processes,
                    platform=platform,
                    baseline_pids=baseline_excel_pids,
                )
            if not killed_specific:
                print(
                    "[main] reward worker timed out without attributed Excel cleanup "
                    f"(fallback_killed={killed_fallback})",
                    file=sys.stderr,
                    flush=True,
                )
            raise RuntimeError(f"timeout after {timeout_s:.0f}s")

        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()

        if not stdout_text:
            print(
                f"[_compute_reward_via_worker] empty worker response. stderr: {stderr_text}",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError("empty worker response")

        try:
            payload = json.loads(stdout_text)
        except Exception as exc:
            print(
                f"[_compute_reward_via_worker] invalid worker response: {stdout_text!r} "
                f"stderr: {stderr_text}",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError("invalid worker response") from exc

        if not isinstance(payload, dict):
            print(
                f"[_compute_reward_via_worker] invalid worker response format: {payload!r} "
                f"stderr: {stderr_text}",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError("invalid worker response format")
        ok = bool(payload.get("ok", False))
        msg = str(payload.get("msg", "") or "")
        if not ok:
            print(
                f"[_compute_reward_via_worker] worker reported failure: {msg} stderr: {stderr_text}",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError("worker reported failure")
        try:
            reward = float(payload.get("reward") or 0.0)
        except (TypeError, ValueError) as exc:
            print(
                f"[_compute_reward_via_worker] invalid worker reward: {payload.get('reward')!r} "
                f"stderr: {stderr_text}",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError("invalid worker reward") from exc
        return reward, msg
    finally:
        await asyncio.to_thread(_unlink_missing_ok, excel_pid_file)


def _get_poll_interval_s() -> float:
    value = os.environ.get("REWARD_API_POLL_INTERVAL_S", "0.2")
    try:
        poll_s = float(value)
        return min(max(0.05, poll_s), 5.0)
    except ValueError:
        return 0.2


def _get_max_running_jobs() -> int | None:
    value = os.environ.get("REWARD_API_MAX_RUNNING_JOBS")
    if value is not None:
        try:
            n = int(value)
            return max(1, n)
        except ValueError:
            return None
    return None


async def _wait_for_terminal_snapshot(job: JobSnapshot, *, wait_s: float) -> JobSnapshot:
    if wait_s <= 0 or job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
        return job

    deadline_s = time.monotonic() + min(wait_s, 25.0)
    poll_interval_s = _get_result_poll_interval_s()
    max_poll_interval_s = _get_result_poll_max_s(poll_interval_s)
    while time.monotonic() < deadline_s:
        remaining_s = max(0.0, deadline_s - time.monotonic())
        await asyncio.sleep(min(poll_interval_s, remaining_s))
        refreshed = await job_manager.get_snapshot(job.job_id)
        if refreshed is None:
            break
        job = refreshed
        if job.status in {JobStatus.DONE, JobStatus.ERROR}:
            break
        poll_interval_s = min(poll_interval_s * 1.5, max_poll_interval_s)
    return job


platform = _get_platform()
job_manager = RewardJobManager(store=SqliteJobStore(_get_db_path()), platform=platform)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await job_manager.start()
    yield
    await job_manager.shutdown()


app = FastAPI(
    title="Async Spreadsheet Reward API",
    version="0.1.0",
    description="Submit+poll reward API backed by Excel recalculation with a shared SQLite job store.",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> JSONResponse:
    stats = await job_manager.stats()
    healthy = bool(stats.get("ready", False))
    status_code = 200 if healthy else 503
    return JSONResponse({"status": "ok" if healthy else "degraded", **stats}, status_code=status_code)


@app.post("/reward/submit")
async def reward_submit(
    thread_dir: str = Form(...),
    file: UploadFile = File(...),
) -> JSONResponse:
    output_root = _get_output_root()
    accepted = False
    proc_file: Path | None = None
    try:
        output_root_resolved = await asyncio.to_thread(_resolve_path, output_root)
        sample_dir = await asyncio.to_thread(_resolve_path, output_root / thread_dir)
    except OSError as exc:
        print(f"[reward_submit] path resolution failed: {_format_exception(exc)}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=500, detail="Failed to resolve configured paths") from exc
    if not sample_dir.is_relative_to(output_root_resolved):
        raise HTTPException(
            status_code=400, detail="Invalid thread_dir: path traversal detected."
        )

    if not await asyncio.to_thread(sample_dir.exists):
        raise HTTPException(status_code=404, detail="Sample directory not found")

    instruction_path = sample_dir / "instruction.json"
    if not await asyncio.to_thread(instruction_path.exists):
        raise HTTPException(status_code=404, detail="instruction.json not found")

    try:
        metadata = await asyncio.to_thread(_get_cached_sample_metadata, sample_dir)
        answer_position = metadata.answer_position
        primary_ext = metadata.primary_ext
    except ValueError as exc:
        print(f"[reward_submit] invalid sample metadata: {_format_exception(exc)}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=400, detail="Invalid sample metadata") from exc
    except RuntimeError as exc:
        print(f"[reward_submit] sample metadata failed: {_format_exception(exc)}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=500, detail="Failed to read sample metadata") from exc

    if primary_ext != "xlsx":
        return JSONResponse({"reward": 0.0, "msg": f"unsupported primary extension '.{primary_ext}'"})

    gt_file = metadata.gt_file
    if not await asyncio.to_thread(_path_exists, gt_file):
        raise HTTPException(status_code=404, detail="Ground-truth file not found")
    try:
        has_capacity = await job_manager.has_queue_capacity()
    except Exception as exc:  # noqa: BLE001
        print(f"[reward_submit] queue preflight failed: {_format_exception(exc)}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=500, detail="Failed to submit reward job") from exc
    if not has_capacity:
        return JSONResponse({"status": "busy", "msg": "queue full"}, status_code=200)

    job_id = uuid.uuid4().hex
    job_dir = sample_dir / ".async_reward_jobs"
    await asyncio.to_thread(_mkdir_parents, job_dir)
    proc_file = job_dir / f"output_{job_id}.{primary_ext}"

    try:
        await file.seek(0)
        await asyncio.to_thread(_copy_upload_to_path, file.file, proc_file)

        job = JobRecord(
            job_id=job_id,
            thread_dir=thread_dir,
            gt_file=gt_file,
            proc_file=proc_file,
            answer_position=answer_position,
        )
        accepted = await job_manager.submit(job)
        if not accepted:
            return JSONResponse({"status": "busy", "msg": "queue full"}, status_code=200)

        return JSONResponse(
            {
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "result_path": f"/reward/result/{job_id}",
                "status_path": f"/reward/status/{job_id}",
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[reward_submit] submit failed: {_format_exception(exc)}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=500, detail="Failed to submit reward job") from exc
    finally:
        if not accepted and proc_file is not None:
            try:
                await asyncio.to_thread(_unlink_missing_ok, proc_file)
            except OSError:
                pass


@app.post("/reward")
async def reward_submit_compat(
    thread_dir: str = Form(...),
    file: UploadFile = File(...),
) -> JSONResponse:
    return await reward_submit(thread_dir=thread_dir, file=file)


@app.post("/recalculate")
async def recalculate_submit_compat(file: UploadFile = File(...)) -> JSONResponse:
    return await recalculate_submit(file=file)


@app.post("/recalculate/submit")
async def recalculate_submit(file: UploadFile = File(...)) -> JSONResponse:
    filename = (file.filename or "").strip() or "workbook.xlsx"
    ext = Path(filename).suffix.lower()
    if ext != ".xlsx":
        raise HTTPException(status_code=400, detail="Only .xlsx files are supported")
    try:
        has_capacity = await job_manager.has_queue_capacity()
    except Exception as exc:  # noqa: BLE001
        print(f"[recalculate_submit] queue preflight failed: {_format_exception(exc)}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=500, detail="Failed to submit recalculate job") from exc
    if not has_capacity:
        return JSONResponse({"status": "busy", "msg": "queue full"}, status_code=200)

    job_id = uuid.uuid4().hex
    job_root = _get_recalculate_job_root()
    await asyncio.to_thread(_mkdir_parents, job_root)
    job_dir = job_root / job_id
    await asyncio.to_thread(_mkdir_parents, job_dir)
    proc_file = job_dir / "workbook.xlsx"

    try:
        try:
            await file.seek(0)
            await asyncio.to_thread(_copy_upload_to_path, file.file, proc_file)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {exc}") from exc

        job = JobRecord(
            job_id=job_id,
            thread_dir="recalculate",
            gt_file=proc_file,
            proc_file=proc_file,
            answer_position="",
            kind=JobKind.RECALCULATE,
        )
        accepted = await job_manager.submit(job)
        if not accepted:
            try:
                await asyncio.to_thread(shutil.rmtree, job_dir)
            except OSError:
                pass
            return JSONResponse({"status": "busy", "msg": "queue full"}, status_code=200)

        return JSONResponse(
            {
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "result_path": f"/recalculate/result/{job_id}",
                "status_path": f"/recalculate/status/{job_id}",
            }
        )
    except HTTPException:
        try:
            await asyncio.to_thread(shutil.rmtree, job_dir)
        except OSError:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            await asyncio.to_thread(shutil.rmtree, job_dir)
        except OSError:
            pass
        print(f"[recalculate_submit] submit failed: {_format_exception(exc)}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=500, detail="Failed to submit recalculate job") from exc


@app.get("/recalculate/status/{job_id}")
async def recalculate_status(job_id: str) -> JSONResponse:
    job = await job_manager.get_snapshot(job_id)
    if job is None or job.kind is not JobKind.RECALCULATE:
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse(
        {
            "job_id": job.job_id,
            "status": job.status.value,
            "created_at_s": job.created_at_s,
            "started_at_s": job.started_at_s,
            "finished_at_s": job.finished_at_s,
        }
    )


@app.get("/recalculate/result/{job_id}")
async def recalculate_result(job_id: str, wait_s: float = 0.0) -> Response:
    job = await job_manager.get_snapshot(job_id)
    if job is None or job.kind is not JobKind.RECALCULATE:
        raise HTTPException(status_code=404, detail="job not found")

    job = await _wait_for_terminal_snapshot(job, wait_s=wait_s)

    if job.status is JobStatus.DONE:
        has_file = await asyncio.to_thread(job.proc_file.is_file)
        if not has_file:
            return JSONResponse(
                {"job_id": job.job_id, "status": JobStatus.ERROR.value, "msg": "result expired"},
                status_code=200,
            )
        return FileResponse(
            path=job.proc_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="workbook.xlsx",
        )

    payload: dict[str, object] = {"job_id": job.job_id, "status": job.status.value}
    if job.status is JobStatus.ERROR:
        payload["msg"] = job.msg or ""
    return JSONResponse(payload)


@app.get("/reward/status/{job_id}")
async def reward_status(job_id: str) -> JSONResponse:
    job = await job_manager.get_snapshot(job_id)
    if job is None or job.kind is not JobKind.REWARD:
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse(
        {
            "job_id": job.job_id,
            "status": job.status.value,
            "created_at_s": job.created_at_s,
            "started_at_s": job.started_at_s,
            "finished_at_s": job.finished_at_s,
        }
    )


@app.get("/reward/result/{job_id}")
async def reward_result(job_id: str, wait_s: float = 0.0) -> JSONResponse:
    job = await job_manager.get_snapshot(job_id)
    if job is None or job.kind is not JobKind.REWARD:
        raise HTTPException(status_code=404, detail="job not found")

    job = await _wait_for_terminal_snapshot(job, wait_s=wait_s)

    payload: dict[str, object] = {"job_id": job.job_id, "status": job.status.value}
    if job.status in {JobStatus.DONE, JobStatus.ERROR}:
        payload["reward"] = float(job.reward or 0.0)
        payload["msg"] = job.msg or ""
    return JSONResponse(payload)
