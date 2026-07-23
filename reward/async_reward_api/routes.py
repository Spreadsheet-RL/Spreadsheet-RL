from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .aio import (
    UploadTooLargeError,
    _await_shielded_ignoring_repeated_cancels,
    _copy_upload_to_path_cancellation_safe,
    _rmtree_with_retries,
    _run_cancellation_safe,
    _unlink_with_retries,
)
from .config import (
    _get_db_path,
    _get_output_root,
    _get_platform,
    _get_recalculate_job_root,
    _get_result_poll_interval_s,
    _get_result_poll_max_s,
)
from .job_store import JobSnapshot, SqliteJobStore
from .manager import RewardJobManager
from .messages import format_exception as _format_exception
from .messages import public_worker_message as _public_worker_message
from .models import JobKind, JobRecord, JobStatus
from .path_safety import (
    _mkdir_parents,
    _path_exists,
    _persisted_recalculate_job_paths_are_safe,
    _resolve_path,
)
from .sample_meta import _get_cached_sample_metadata

logger = logging.getLogger(__name__)


def _file_chunks(file_obj, chunk_size: int = 1024 * 1024):
    try:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        file_obj.close()


def _open_binary(path: Path):
    return path.open("rb")

def _validate_wait_s(wait_s: float) -> float:
    try:
        value = float(wait_s)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="wait_s must be finite and non-negative") from exc
    if not math.isfinite(value) or value < 0:
        raise HTTPException(status_code=400, detail="wait_s must be finite and non-negative")
    return value


def _finite_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _public_job_msg(msg: object) -> str:
    return _public_worker_message(msg, fallback="")


class _SubmitPreflightError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        detail: str,
        log_context: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = detail
        self.log_context = log_context
        self.cause = cause


@dataclass(frozen=True)
class _RewardSubmitPreflight:
    output_root_resolved: Path
    sample_dir: Path
    gt_file: Path
    answer_position: str
    primary_ext: str


@dataclass(frozen=True)
class _RewardJobPaths:
    proc_file: Path


@dataclass(frozen=True)
class _RecalculateJobPaths:
    job_root: Path
    job_dir: Path
    proc_file: Path


def _raise_submit_preflight_http(context: str, exc: _SubmitPreflightError) -> NoReturn:
    if exc.log_context is not None:
        logger.warning(
            f"[{context}] {exc.log_context}: {_format_exception(exc.cause or exc)}"
        )
    if exc.cause is not None:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc.cause
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _reward_submit_preflight_sync(thread_dir: str) -> _RewardSubmitPreflight:
    output_root = _get_output_root()
    try:
        output_root_resolved = _resolve_path(output_root)
    except (OSError, ValueError) as exc:
        raise _SubmitPreflightError(
            status_code=500,
            detail="Failed to resolve configured paths",
            log_context="output root resolution failed",
            cause=exc,
        ) from exc
    try:
        sample_dir = _resolve_path(output_root / thread_dir)
    except ValueError as exc:
        raise _SubmitPreflightError(
            status_code=400,
            detail="Invalid thread_dir: malformed path.",
            log_context="invalid thread_dir path",
            cause=exc,
        ) from exc
    except OSError as exc:
        raise _SubmitPreflightError(
            status_code=500,
            detail="Failed to resolve configured paths",
            log_context="path resolution failed",
            cause=exc,
        ) from exc
    if not sample_dir.is_relative_to(output_root_resolved):
        raise _SubmitPreflightError(
            status_code=400,
            detail="Invalid thread_dir: path traversal detected.",
        )

    if not _path_exists(sample_dir):
        raise _SubmitPreflightError(status_code=404, detail="Sample directory not found")

    instruction_path = sample_dir / "instruction.json"
    if not _path_exists(instruction_path):
        raise _SubmitPreflightError(status_code=404, detail="instruction.json not found")

    try:
        metadata = _get_cached_sample_metadata(sample_dir)
    except ValueError as exc:
        raise _SubmitPreflightError(
            status_code=400,
            detail="Invalid sample metadata",
            log_context="invalid sample metadata",
            cause=exc,
        ) from exc
    except RuntimeError as exc:
        raise _SubmitPreflightError(
            status_code=500,
            detail="Failed to read sample metadata",
            log_context="sample metadata failed",
            cause=exc,
        ) from exc

    primary_ext = metadata.primary_ext
    if primary_ext != "xlsx":
        raise _SubmitPreflightError(
            status_code=400,
            detail=f"Unsupported primary extension '.{primary_ext}'",
        )

    gt_file = metadata.gt_file
    if not _path_exists(gt_file):
        raise _SubmitPreflightError(status_code=404, detail="Ground-truth file not found")
    try:
        gt_file_resolved = _resolve_path(gt_file)
    except (OSError, ValueError) as exc:
        raise _SubmitPreflightError(
            status_code=400,
            detail="Invalid sample metadata",
            log_context="target path resolution failed",
            cause=exc,
        ) from exc
    if not gt_file_resolved.is_relative_to(output_root_resolved):
        raise _SubmitPreflightError(status_code=400, detail="Invalid sample metadata")

    return _RewardSubmitPreflight(
        output_root_resolved=output_root_resolved,
        sample_dir=sample_dir,
        gt_file=gt_file,
        answer_position=metadata.answer_position,
        primary_ext=primary_ext,
    )


def _reward_job_paths_sync(
    sample_dir: Path,
    job_id: str,
    primary_ext: str,
    output_root_resolved: Path,
) -> _RewardJobPaths:
    job_dir = sample_dir / ".async_reward_jobs"
    proc_file = job_dir / f"output_{job_id}.{primary_ext}"
    _mkdir_parents(job_dir)
    try:
        job_dir_resolved = _resolve_path(job_dir)
        proc_file_resolved = _resolve_path(proc_file)
    except (OSError, ValueError) as exc:
        raise _SubmitPreflightError(
            status_code=500,
            detail="Failed to resolve configured paths",
            log_context="job path resolution failed",
            cause=exc,
        ) from exc
    if (
        not job_dir_resolved.is_relative_to(output_root_resolved)
        or not proc_file_resolved.is_relative_to(output_root_resolved)
    ):
        raise _SubmitPreflightError(
            status_code=400,
            detail="Invalid thread_dir: path traversal detected.",
        )
    return _RewardJobPaths(proc_file=proc_file)


def _recalculate_job_paths_sync(job_id: str) -> _RecalculateJobPaths:
    job_root = _get_recalculate_job_root()
    job_dir = job_root / job_id
    proc_file = job_dir / "workbook.xlsx"
    _mkdir_parents(job_root)
    _mkdir_parents(job_dir)
    try:
        job_root_resolved = _resolve_path(job_root)
        job_dir_resolved = _resolve_path(job_dir)
        proc_file_resolved = _resolve_path(proc_file)
    except (OSError, ValueError) as exc:
        raise _SubmitPreflightError(
            status_code=500,
            detail="Failed to resolve configured paths",
            log_context="job path resolution failed",
            cause=exc,
        ) from exc
    if (
        not job_dir_resolved.is_relative_to(job_root_resolved)
        or not proc_file_resolved.is_relative_to(job_root_resolved)
    ):
        raise _SubmitPreflightError(
            status_code=500,
            detail="Failed to resolve configured paths",
        )
    return _RecalculateJobPaths(job_root=job_root, job_dir=job_dir, proc_file=proc_file)


async def _get_snapshot_or_503(job_id: str, *, context: str) -> JobSnapshot | None:
    try:
        return await job_manager.get_snapshot(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{context}] snapshot failed: {_format_exception(exc)}")
        raise HTTPException(status_code=503, detail="job store temporarily unavailable") from exc


async def _wait_for_terminal_snapshot(job: JobSnapshot, *, wait_s: float) -> JobSnapshot:
    wait_s = _validate_wait_s(wait_s)
    if wait_s <= 0 or job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
        return job

    deadline_s = time.monotonic() + min(wait_s, 25.0)
    poll_interval_s = _get_result_poll_interval_s()
    max_poll_interval_s = _get_result_poll_max_s(poll_interval_s)
    while time.monotonic() < deadline_s:
        remaining_s = max(0.0, deadline_s - time.monotonic())
        await asyncio.sleep(min(poll_interval_s, remaining_s))
        refreshed = await _get_snapshot_or_503(job.job_id, context="result_wait")
        if refreshed is None:
            raise HTTPException(status_code=404, detail="job not found")
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
    try:
        stats = await job_manager.stats()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[health] stats failed: {_format_exception(exc)}")
        return JSONResponse(
            {"status": "degraded", "ready": False, "error": "stats unavailable"},
            status_code=503,
        )
    healthy = bool(stats.get("ready", False))
    status_code = 200 if healthy else 503
    return JSONResponse({"status": "ok" if healthy else "degraded", **stats}, status_code=status_code)


@app.post("/reward/submit")
async def reward_submit(
    thread_dir: str = Form(...),
    file: UploadFile = File(...),
) -> JSONResponse:
    accepted = False
    proc_file: Path | None = None
    proc_file_cleanup_allowed = False
    try:
        preflight = await asyncio.to_thread(_reward_submit_preflight_sync, thread_dir)
    except _SubmitPreflightError as exc:
        _raise_submit_preflight_http("reward_submit", exc)
    try:
        has_capacity = await job_manager.has_queue_capacity()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[reward_submit] queue preflight failed: {_format_exception(exc)}")
        raise HTTPException(status_code=503, detail="job store temporarily unavailable") from exc
    if not has_capacity:
        return JSONResponse({"status": "busy", "msg": "queue full"}, status_code=200)

    job_id = uuid.uuid4().hex

    try:
        try:
            paths = await asyncio.to_thread(
                _reward_job_paths_sync,
                preflight.sample_dir,
                job_id,
                preflight.primary_ext,
                preflight.output_root_resolved,
            )
        except _SubmitPreflightError as exc:
            _raise_submit_preflight_http("reward_submit", exc)
        proc_file = paths.proc_file
        proc_file_cleanup_allowed = True
        await file.seek(0)
        await _copy_upload_to_path_cancellation_safe(
            file.file,
            proc_file,
            context="reward_submit",
        )

        job = JobRecord(
            job_id=job_id,
            thread_dir=thread_dir,
            gt_file=preflight.gt_file,
            proc_file=proc_file,
            answer_position=preflight.answer_position,
        )
        submit_task = asyncio.create_task(job_manager.submit(job))
        try:
            accepted = await asyncio.shield(submit_task)
        except asyncio.CancelledError:
            try:
                accepted = await _await_shielded_ignoring_repeated_cancels(submit_task)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[reward_submit] submit failed after request cancellation: {_format_exception(exc)}")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[reward_submit] submit failed: {_format_exception(exc)}")
            raise HTTPException(status_code=503, detail="job store temporarily unavailable") from exc
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
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail="Uploaded file too large") from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[reward_submit] submit failed: {_format_exception(exc)}")
        raise HTTPException(status_code=500, detail="Failed to submit reward job") from exc
    finally:
        if not accepted and proc_file is not None and proc_file_cleanup_allowed:
            delete_delays_s = (0.0, 0.25, 1.0) if os.name == "nt" else (0.0,)
            try:
                cleaned = await _run_cancellation_safe(_unlink_with_retries(proc_file, delete_delays_s))
            except asyncio.CancelledError:
                raise
            if not cleaned:
                logger.warning(f"[reward_submit] failed to clean up rejected upload: {proc_file}")


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
        logger.warning(f"[recalculate_submit] queue preflight failed: {_format_exception(exc)}")
        raise HTTPException(status_code=503, detail="job store temporarily unavailable") from exc
    if not has_capacity:
        return JSONResponse({"status": "busy", "msg": "queue full"}, status_code=200)

    job_id = uuid.uuid4().hex
    accepted = False
    job_dir_cleanup_allowed = False

    try:
        try:
            paths = await asyncio.to_thread(_recalculate_job_paths_sync, job_id)
        except _SubmitPreflightError as exc:
            _raise_submit_preflight_http("recalculate_submit", exc)
        job_dir = paths.job_dir
        proc_file = paths.proc_file
        job_dir_cleanup_allowed = True
        try:
            await file.seek(0)
            await _copy_upload_to_path_cancellation_safe(
                file.file,
                proc_file,
                context="recalculate_submit",
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail="Uploaded file too large") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[recalculate_submit] upload save failed: {_format_exception(exc)}")
            raise HTTPException(status_code=500, detail="Failed to save uploaded file") from exc

        job = JobRecord(
            job_id=job_id,
            thread_dir="recalculate",
            gt_file=paths.job_root,
            proc_file=proc_file,
            answer_position="",
            kind=JobKind.RECALCULATE,
        )
        submit_task = asyncio.create_task(job_manager.submit(job))
        try:
            accepted = await asyncio.shield(submit_task)
        except asyncio.CancelledError:
            try:
                accepted = await _await_shielded_ignoring_repeated_cancels(submit_task)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[recalculate_submit] submit failed after request cancellation: {_format_exception(exc)}")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[recalculate_submit] submit failed: {_format_exception(exc)}")
            raise HTTPException(status_code=503, detail="job store temporarily unavailable") from exc
        if not accepted:
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
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[recalculate_submit] submit failed: {_format_exception(exc)}")
        raise HTTPException(status_code=500, detail="Failed to submit recalculate job") from exc
    finally:
        if not accepted and job_dir_cleanup_allowed:
            delete_delays_s = (0.0, 0.25, 1.0) if os.name == "nt" else (0.0,)
            try:
                cleaned = await _run_cancellation_safe(_rmtree_with_retries(job_dir, delete_delays_s))
            except asyncio.CancelledError:
                raise
            if not cleaned:
                logger.warning(f"[recalculate_submit] failed to clean up rejected job dir: {job_dir}")


@app.get("/recalculate/status/{job_id}")
async def recalculate_status(job_id: str) -> JSONResponse:
    job = await _get_snapshot_or_503(job_id, context="recalculate_status")
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
    job = await _get_snapshot_or_503(job_id, context="recalculate_result")
    if job is None or job.kind is not JobKind.RECALCULATE:
        raise HTTPException(status_code=404, detail="job not found")

    job = await _wait_for_terminal_snapshot(job, wait_s=wait_s)

    if job.status is JobStatus.DONE:
        if not _persisted_recalculate_job_paths_are_safe(job):
            logger.warning(f"[recalculate_result] refusing unexpected result path for job {job.job_id}")
            try:
                await job_manager.mark_result_error(
                    job_id=job.job_id,
                    kind=JobKind.RECALCULATE,
                    msg="invalid result path",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[recalculate_result] failed to persist invalid result for job {job.job_id}: "
                    f"{_format_exception(exc)}")
            return JSONResponse(
                {"job_id": job.job_id, "status": JobStatus.ERROR.value, "msg": "invalid result path"},
                status_code=200,
            )
        try:
            file_obj = await asyncio.to_thread(_open_binary, job.proc_file)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            expired_persisted = False
            expire_attempt_failed = False
            try:
                expired_persisted = await job_manager.mark_result_expired(
                    job_id=job.job_id,
                    kind=JobKind.RECALCULATE,
                )
            except Exception as exc:  # noqa: BLE001
                expire_attempt_failed = True
                logger.warning(f"[recalculate_result] failed to persist expired result for job {job.job_id}: "
                    f"{_format_exception(exc)}")
            if not expired_persisted and not expire_attempt_failed:
                refreshed = await _get_snapshot_or_503(job.job_id, context="recalculate_result")
                if refreshed is None or refreshed.kind is not JobKind.RECALCULATE:
                    raise HTTPException(status_code=404, detail="job not found")
                if refreshed.status is not JobStatus.DONE:
                    return JSONResponse(
                        {
                            "job_id": refreshed.job_id,
                            "status": refreshed.status.value,
                            "msg": _public_job_msg(refreshed.msg),
                        },
                        status_code=200,
                    )
            return JSONResponse(
                {"job_id": job.job_id, "status": JobStatus.ERROR.value, "msg": "result expired"},
                status_code=200,
            )
        except OSError as exc:
            logger.warning(f"[recalculate_result] result temporarily unavailable for job {job.job_id}: {_format_exception(exc)}")
            raise HTTPException(status_code=503, detail="result temporarily unavailable") from exc
        return StreamingResponse(
            _file_chunks(file_obj),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="workbook.xlsx"'},
        )

    payload: dict[str, object] = {"job_id": job.job_id, "status": job.status.value}
    if job.status is JobStatus.ERROR:
        payload["msg"] = _public_job_msg(job.msg)
    return JSONResponse(payload)


@app.get("/reward/status/{job_id}")
async def reward_status(job_id: str) -> JSONResponse:
    job = await _get_snapshot_or_503(job_id, context="reward_status")
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
    job = await _get_snapshot_or_503(job_id, context="reward_result")
    if job is None or job.kind is not JobKind.REWARD:
        raise HTTPException(status_code=404, detail="job not found")

    job = await _wait_for_terminal_snapshot(job, wait_s=wait_s)

    payload: dict[str, object] = {"job_id": job.job_id, "status": job.status.value}
    if job.status in {JobStatus.DONE, JobStatus.ERROR}:
        reward = _finite_float_or_none(job.reward)
        invalid_reward = (job.status is JobStatus.DONE and job.reward is None) or (
            job.reward is not None and reward is None
        )
        if invalid_reward:
            logger.warning(f"[reward_result] invalid persisted reward for job {job.job_id}")
        invalid_row_persisted = False

        async def _persist_invalid_row() -> bool:
            try:
                return await job_manager.mark_result_error(
                    job_id=job.job_id,
                    kind=JobKind.REWARD,
                    msg="invalid persisted job row",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[reward_result] failed to persist invalid reward for job {job.job_id}: {_format_exception(exc)}")
                return False

        if job.quarantined_invalid:
            invalid_row_persisted = await _persist_invalid_row()
        if job.status is JobStatus.DONE and invalid_reward:
            if not invalid_row_persisted:
                await _persist_invalid_row()
            payload["status"] = JobStatus.ERROR.value
            payload["reward"] = 0.0
            payload["msg"] = "invalid persisted job row"
        else:
            payload["reward"] = reward if reward is not None else 0.0
            payload["msg"] = _public_job_msg(job.msg) or ("invalid persisted job row" if invalid_reward else "")
    return JSONResponse(payload)
