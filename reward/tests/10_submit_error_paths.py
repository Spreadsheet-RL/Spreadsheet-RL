from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from _tempdir import temporary_directory

os.environ["REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"] = "1"
os.environ["REWARD_API_PLATFORM"] = "windows"

from fastapi import HTTPException, UploadFile  # noqa: E402

from async_reward_api import aio as aio_mod  # noqa: E402
from async_reward_api import main as main_mod  # noqa: E402
from async_reward_api import routes as routes_mod  # noqa: E402
from async_reward_api import sample_meta as sample_meta_mod  # noqa: E402


class _FakeManager:
    async def has_queue_capacity(self) -> bool:
        return True

    async def submit(self, job) -> bool:
        raise AssertionError(f"submit should not be reached after mkdir failure: {job}")


class _BusyManager:
    async def has_queue_capacity(self) -> bool:
        return True

    async def submit(self, job) -> bool:
        return False


class _CapacityFailManager:
    async def has_queue_capacity(self) -> bool:
        raise RuntimeError("capacity failed")


class _SubmitFailManager:
    async def has_queue_capacity(self) -> bool:
        return True

    async def submit(self, job) -> bool:
        raise RuntimeError("submit failed")


class _DelayedAcceptManager:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.job = None

    async def has_queue_capacity(self) -> bool:
        return True

    async def submit(self, job) -> bool:
        self.job = job
        self.started.set()
        await self.release.wait()
        return self.accepted


class _SnapshotManager:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.mark_result_error_calls: list[dict[str, object]] = []

    async def get_snapshot(self, job_id: str):
        return self.snapshot if self.snapshot.job_id == job_id else None

    async def mark_result_error(self, **kwargs) -> bool:
        self.mark_result_error_calls.append(kwargs)
        return True


class _FailSnapshotManager:
    async def get_snapshot(self, job_id: str):
        raise RuntimeError("snapshot failed for C:\\secret\\jobs.sqlite3")


class _FailRefreshManager:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def get_snapshot(self, job_id: str):
        self.calls += 1
        if self.calls == 1 and self.snapshot.job_id == job_id:
            return self.snapshot
        raise RuntimeError("snapshot refresh failed for C:\\secret\\jobs.sqlite3")


class _DisappearRefreshManager:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def get_snapshot(self, job_id: str):
        self.calls += 1
        if self.calls == 1 and self.snapshot.job_id == job_id:
            return self.snapshot
        return None


class _StatsFailManager:
    async def stats(self) -> dict[str, object]:
        raise RuntimeError("stats failed for C:\\secret\\jobs.sqlite3")


class _MarkExpiredFailManager(_SnapshotManager):
    async def mark_result_expired(self, **kwargs) -> bool:
        raise RuntimeError(f"mark_result_expired failed: {kwargs}")


class _MarkExpiredNoopManager(_SnapshotManager):
    async def mark_result_expired(self, **kwargs) -> bool:
        return False


class _MarkExpiredRaceManager(_SnapshotManager):
    async def mark_result_expired(self, **kwargs) -> bool:
        self.snapshot = replace(
            self.snapshot,
            status=main_mod.JobStatus.ERROR,
            msg="result expired",
        )
        return False


class _MarkExpiredDeletedManager(_SnapshotManager):
    async def get_snapshot(self, job_id: str):
        return None if self.snapshot is None else await super().get_snapshot(job_id)

    async def mark_result_expired(self, **kwargs) -> bool:
        self.snapshot = None
        return False


class _BlockingCopy:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, source, destination: Path) -> None:
        with destination.open("wb") as output:
            output.write(b"partial")
            output.flush()
            self.started.set()
            self.release.wait(timeout=5.0)
            output.write(b"complete")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _upload() -> UploadFile:
    return UploadFile(file=io.BytesIO(b"not actually an xlsx"), filename="workbook.xlsx")


def _upload_bytes(payload: bytes, *, filename: str = "workbook.xlsx") -> UploadFile:
    return UploadFile(file=io.BytesIO(payload), filename=filename)


async def _expect_http_error(label: str, coro, *, status_code: int, detail: str) -> None:
    try:
        await coro
    except HTTPException as exc:
        _assert(exc.status_code == status_code, f"{label}: status={exc.status_code}")
        _assert(exc.detail == detail, f"{label}: detail={exc.detail!r}")
        return
    raise AssertionError(f"{label}: expected HTTPException")


async def main_async() -> int:
    with temporary_directory(prefix="async_reward_api_submit_errors_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        original_mkdir = routes_mod._mkdir_parents
        original_unlink_with_retries = routes_mod._unlink_with_retries
        original_rmtree_with_retries = routes_mod._rmtree_with_retries

        def fail_mkdir(path: Path) -> None:
            raise OSError(f"mkdir failed for {path.name}")

        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        routes_mod.job_manager = _FakeManager()
        routes_mod._mkdir_parents = fail_mkdir
        try:
            await _expect_http_error(
                "malformed reward thread_dir",
                main_mod.reward_submit(thread_dir="bad\x00dir", file=_upload()),
                status_code=400,
                detail="Invalid thread_dir: malformed path.",
            )
            await _expect_http_error(
                "reward mkdir failure",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=500,
                detail="Failed to submit reward job",
            )
            await _expect_http_error(
                "recalculate mkdir failure",
                main_mod.recalculate_submit(file=_upload()),
                status_code=500,
                detail="Failed to submit recalculate job",
            )
        finally:
            routes_mod._mkdir_parents = original_mkdir
            routes_mod.job_manager = original_manager
            routes_mod._unlink_with_retries = original_unlink_with_retries
            routes_mod._rmtree_with_retries = original_rmtree_with_retries
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_submit_out_of_root_jobs_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        outside_jobs_dir = tmp_path / "outside_jobs"
        sample_dir.mkdir(parents=True)
        outside_jobs_dir.mkdir()
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")
        job_dir = sample_dir / ".async_reward_jobs"
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        original_resolve_path = routes_mod._resolve_path
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = _FakeManager()
        symlink_created = False
        try:
            try:
                os.symlink(outside_jobs_dir, job_dir, target_is_directory=True)
                symlink_created = True
            except (OSError, NotImplementedError):
                pass

            if not symlink_created:
                def fake_resolve_path(path: Path) -> Path:
                    if path == job_dir:
                        return outside_jobs_dir.resolve()
                    if path.parent == job_dir:
                        return outside_jobs_dir.resolve() / path.name
                    return original_resolve_path(path)

                routes_mod._resolve_path = fake_resolve_path

            await _expect_http_error(
                "out-of-root reward job dir",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=400,
                detail="Invalid thread_dir: path traversal detected.",
            )
            _assert(
                not any(outside_jobs_dir.iterdir()),
                "out-of-root reward submit guard wrote outside output root",
            )
        finally:
            routes_mod._resolve_path = original_resolve_path
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_submit_out_of_root_target_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        outside_target = tmp_path / "outside_target.xlsx"
        sample_dir.mkdir(parents=True)
        outside_target.write_bytes(b"placeholder")
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        original_get_cached_sample_metadata = routes_mod._get_cached_sample_metadata
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = _FakeManager()
        routes_mod._get_cached_sample_metadata = lambda sample_dir: sample_meta_mod._RewardSampleMetadata(
            answer_position="Sheet1!A1",
            primary_ext="xlsx",
            gt_file=outside_target,
        )
        try:
            await _expect_http_error(
                "out-of-root reward target",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=400,
                detail="Invalid sample metadata",
            )
        finally:
            routes_mod._get_cached_sample_metadata = original_get_cached_sample_metadata
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_submit_invalid_metadata_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "not-a-range"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = _FakeManager()
        try:
            await _expect_http_error(
                "invalid reward metadata",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=400,
                detail="Invalid sample metadata",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_submit_bad_instruction_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = _FakeManager()
        try:
            (sample_dir / "instruction.json").write_text("{", encoding="utf-8")
            await _expect_http_error(
                "malformed instruction json",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=400,
                detail="Invalid sample metadata",
            )
            (sample_dir / "instruction.json").write_text(json.dumps({"other": "Sheet1!A1"}), encoding="utf-8")
            await _expect_http_error(
                "missing answer position instruction",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=400,
                detail="Invalid sample metadata",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_submit_store_unavailable_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        try:
            routes_mod.job_manager = _CapacityFailManager()
            await _expect_http_error(
                "reward capacity store failure",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=503,
                detail="job store temporarily unavailable",
            )
            await _expect_http_error(
                "recalculate capacity store failure",
                main_mod.recalculate_submit(file=_upload()),
                status_code=503,
                detail="job store temporarily unavailable",
            )

            routes_mod.job_manager = _SubmitFailManager()
            await _expect_http_error(
                "reward submit store failure",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=503,
                detail="job store temporarily unavailable",
            )
            await _expect_http_error(
                "recalculate submit store failure",
                main_mod.recalculate_submit(file=_upload()),
                status_code=503,
                detail="job store temporarily unavailable",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_submit_unsupported_target_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.csv").write_bytes(b"answer\n1\n")
        (sample_dir / "unrelated.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = _FakeManager()
        try:
            await _expect_http_error(
                "unsupported target extension",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=400,
                detail="Unsupported primary extension '.csv'",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_submit_missing_target_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "output.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = _FakeManager()
        try:
            await _expect_http_error(
                "missing target attachment",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload()),
                status_code=400,
                detail="Invalid sample metadata",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_target_case_") as tmp:
        sample_dir = Path(tmp)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        target_path = sample_dir / "Target.xlsx"
        target_path.write_bytes(b"placeholder")
        metadata = sample_meta_mod._resolve_sample_metadata(sample_dir)
        _assert(metadata.gt_file == target_path, f"target filename case was not preserved: {metadata.gt_file}")

    with temporary_directory(prefix="async_reward_api_sample_meta_inflight_") as tmp:
        sample_dir = Path(tmp)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")
        original_resolve_sample_metadata = sample_meta_mod._resolve_sample_metadata
        calls = {"count": 0}
        calls_lock = threading.Lock()
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def slow_resolve(path: Path):
            with calls_lock:
                calls["count"] += 1
            threading.Event().wait(0.05)
            return original_resolve_sample_metadata(path)

        def load_metadata() -> None:
            try:
                barrier.wait(timeout=5.0)
                metadata = sample_meta_mod._get_cached_sample_metadata(sample_dir)
                if metadata.gt_file != sample_dir / "target.xlsx":
                    raise AssertionError(f"unexpected metadata: {metadata}")
            except BaseException as exc:
                errors.append(exc)

        with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
            sample_meta_mod._SAMPLE_META_CACHE.clear()
            sample_meta_mod._SAMPLE_META_INFLIGHT.clear()
        sample_meta_mod._resolve_sample_metadata = slow_resolve
        try:
            threads = [threading.Thread(target=load_metadata) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5.0)
            if errors:
                raise AssertionError(f"concurrent sample metadata load failed: {errors!r}")
            _assert(calls["count"] == 1, f"sample metadata resolved {calls['count']} times")
        finally:
            sample_meta_mod._resolve_sample_metadata = original_resolve_sample_metadata
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                sample_meta_mod._SAMPLE_META_CACHE.clear()
                sample_meta_mod._SAMPLE_META_INFLIGHT.clear()

    with temporary_directory(prefix="async_reward_api_sample_meta_stale_inflight_") as tmp:
        sample_dir = Path(tmp)
        instruction_path = sample_dir / "instruction.json"
        instruction_path.write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        target_path = sample_dir / "target.xlsx"
        target_path.write_bytes(b"placeholder")
        cache_key = (
            str(sample_dir.resolve()),
            int(sample_dir.stat().st_mtime_ns),
            int(instruction_path.stat().st_mtime_ns),
        )
        original_wait_s = sample_meta_mod._SAMPLE_META_INFLIGHT_WAIT_S
        sample_meta_mod._SAMPLE_META_INFLIGHT_WAIT_S = 0.01
        with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
            sample_meta_mod._SAMPLE_META_CACHE.clear()
            sample_meta_mod._SAMPLE_META_INFLIGHT.clear()
            sample_meta_mod._SAMPLE_META_INFLIGHT[cache_key] = sample_meta_mod._SampleMetadataInflight(
                event=threading.Event()
            )
        try:
            metadata = sample_meta_mod._get_cached_sample_metadata(sample_dir)
            _assert(
                metadata.gt_file == target_path,
                f"stale sample metadata inflight did not fall back to local resolve: {metadata}",
            )
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                _assert(
                    cache_key not in sample_meta_mod._SAMPLE_META_INFLIGHT,
                    "stale sample metadata inflight was not removed after timeout fallback",
                )
        finally:
            sample_meta_mod._SAMPLE_META_INFLIGHT_WAIT_S = original_wait_s
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                sample_meta_mod._SAMPLE_META_CACHE.clear()
                sample_meta_mod._SAMPLE_META_INFLIGHT.clear()

    with temporary_directory(prefix="async_reward_api_sample_meta_inflight_identity_success_") as tmp:
        sample_dir = Path(tmp)
        instruction_path = sample_dir / "instruction.json"
        instruction_path.write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        target_path = sample_dir / "target.xlsx"
        target_path.write_bytes(b"placeholder")
        cache_key = (
            str(sample_dir.resolve()),
            int(sample_dir.stat().st_mtime_ns),
            int(instruction_path.stat().st_mtime_ns),
        )
        replacement_inflight = sample_meta_mod._SampleMetadataInflight(event=threading.Event())
        metadata = sample_meta_mod._RewardSampleMetadata(
            answer_position="Sheet1!A1",
            primary_ext="xlsx",
            gt_file=target_path,
        )
        original_resolve_sample_metadata = sample_meta_mod._resolve_sample_metadata

        def replacing_resolve(path: Path):
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                sample_meta_mod._SAMPLE_META_INFLIGHT[cache_key] = replacement_inflight
            return metadata

        sample_meta_mod._resolve_sample_metadata = replacing_resolve
        with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
            sample_meta_mod._SAMPLE_META_CACHE.clear()
            sample_meta_mod._SAMPLE_META_INFLIGHT.clear()
        try:
            loaded = sample_meta_mod._get_cached_sample_metadata(sample_dir)
            _assert(loaded == metadata, f"sample metadata identity success result changed: {loaded}")
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                _assert(
                    sample_meta_mod._SAMPLE_META_INFLIGHT.get(cache_key) is replacement_inflight,
                    "sample metadata identity success removed newer inflight",
                )
        finally:
            sample_meta_mod._resolve_sample_metadata = original_resolve_sample_metadata
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                sample_meta_mod._SAMPLE_META_CACHE.clear()
                sample_meta_mod._SAMPLE_META_INFLIGHT.clear()

    with temporary_directory(prefix="async_reward_api_sample_meta_inflight_identity_error_") as tmp:
        sample_dir = Path(tmp)
        instruction_path = sample_dir / "instruction.json"
        instruction_path.write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")
        cache_key = (
            str(sample_dir.resolve()),
            int(sample_dir.stat().st_mtime_ns),
            int(instruction_path.stat().st_mtime_ns),
        )
        replacement_inflight = sample_meta_mod._SampleMetadataInflight(event=threading.Event())
        expected_resolve_error = RuntimeError("resolve failed")
        original_resolve_sample_metadata = sample_meta_mod._resolve_sample_metadata

        def replacing_failing_resolve(path: Path):
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                sample_meta_mod._SAMPLE_META_INFLIGHT[cache_key] = replacement_inflight
            raise expected_resolve_error

        sample_meta_mod._resolve_sample_metadata = replacing_failing_resolve
        with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
            sample_meta_mod._SAMPLE_META_CACHE.clear()
            sample_meta_mod._SAMPLE_META_INFLIGHT.clear()
        try:
            try:
                sample_meta_mod._get_cached_sample_metadata(sample_dir)
            except RuntimeError as exc:
                if exc is not expected_resolve_error:
                    raise AssertionError(f"unexpected sample metadata error: {exc!r}") from exc
            else:
                raise AssertionError("sample metadata resolve error did not propagate")
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                _assert(
                    sample_meta_mod._SAMPLE_META_INFLIGHT.get(cache_key) is replacement_inflight,
                    "sample metadata identity exception removed newer inflight",
                )
        finally:
            sample_meta_mod._resolve_sample_metadata = original_resolve_sample_metadata
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                sample_meta_mod._SAMPLE_META_CACHE.clear()
                sample_meta_mod._SAMPLE_META_INFLIGHT.clear()

    with temporary_directory(prefix="async_reward_api_sample_meta_stale_target_") as tmp:
        sample_dir = Path(tmp)
        instruction_path = sample_dir / "instruction.json"
        instruction_path.write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        stale_target = sample_dir / "target.xlsx"
        fresh_target = sample_dir / "target.csv"
        fresh_target.write_bytes(b"answer\n1\n")
        cache_key = (
            str(sample_dir.resolve()),
            int(sample_dir.stat().st_mtime_ns),
            int(instruction_path.stat().st_mtime_ns),
        )
        stale_metadata = sample_meta_mod._RewardSampleMetadata(
            answer_position="Sheet1!A1",
            primary_ext="xlsx",
            gt_file=stale_target,
        )
        with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
            sample_meta_mod._SAMPLE_META_CACHE.clear()
            sample_meta_mod._SAMPLE_META_INFLIGHT.clear()
            sample_meta_mod._SAMPLE_META_CACHE[cache_key] = stale_metadata
        try:
            metadata = sample_meta_mod._get_cached_sample_metadata(sample_dir)
            _assert(
                metadata.gt_file == fresh_target,
                f"stale cached metadata target was not recomputed: {metadata}",
            )
            _assert(metadata.primary_ext == "csv", f"recomputed metadata extension changed: {metadata}")
        finally:
            with sample_meta_mod._SAMPLE_META_CACHE_LOCK:
                sample_meta_mod._SAMPLE_META_CACHE.clear()
                sample_meta_mod._SAMPLE_META_INFLIGHT.clear()

    with temporary_directory(prefix="async_reward_api_upload_limit_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_max_upload = os.environ.get("REWARD_API_MAX_UPLOAD_BYTES")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        os.environ["REWARD_API_MAX_UPLOAD_BYTES"] = "4"
        routes_mod.job_manager = _BusyManager()
        try:
            await _expect_http_error(
                "oversized reward upload",
                main_mod.reward_submit(thread_dir="thread_1", file=_upload_bytes(b"12345")),
                status_code=413,
                detail="Uploaded file too large",
            )
            job_dir = sample_dir / ".async_reward_jobs"
            leaked_reward_files = list(job_dir.glob("*")) if job_dir.exists() else []
            _assert(not leaked_reward_files, f"oversized reward upload left files: {leaked_reward_files}")

            await _expect_http_error(
                "oversized recalculate upload",
                main_mod.recalculate_submit(file=_upload_bytes(b"12345")),
                status_code=413,
                detail="Uploaded file too large",
            )
            recalc_root = Path(os.environ["REWARD_API_RECALC_JOB_ROOT"])
            leaked_recalc_entries = list(recalc_root.iterdir()) if recalc_root.exists() else []
            _assert(not leaked_recalc_entries, f"oversized recalculate upload left files: {leaked_recalc_entries}")
        finally:
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root
            if original_max_upload is None:
                os.environ.pop("REWARD_API_MAX_UPLOAD_BYTES", None)
            else:
                os.environ["REWARD_API_MAX_UPLOAD_BYTES"] = original_max_upload

    with temporary_directory(prefix="async_reward_api_recalc_submit_out_of_root_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        outside_root = tmp_path / "outside_recalc_jobs"
        recalc_root.mkdir()
        outside_root.mkdir()
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        original_resolve_path = routes_mod._resolve_path
        original_copy_upload_to_path = aio_mod._copy_upload_to_path
        copy_calls = {"count": 0}

        def fake_resolve_path(path: Path) -> Path:
            if path == recalc_root:
                return original_resolve_path(path)
            if path.parent == recalc_root:
                return outside_root.resolve() / path.name
            if path.name == "workbook.xlsx" and path.parent.parent == recalc_root:
                return outside_root.resolve() / path.parent.name / path.name
            return original_resolve_path(path)

        def record_copy(source, destination: Path) -> None:
            copy_calls["count"] += 1
            raise AssertionError(f"copy should not run for out-of-root recalc path: {destination}")

        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        routes_mod.job_manager = _FakeManager()
        routes_mod._resolve_path = fake_resolve_path
        aio_mod._copy_upload_to_path = record_copy
        try:
            await _expect_http_error(
                "out-of-root recalculate job dir",
                main_mod.recalculate_submit(file=_upload()),
                status_code=500,
                detail="Failed to resolve configured paths",
            )
            _assert(copy_calls["count"] == 0, "recalculate submit copied before path-root validation")
            _assert(not any(outside_root.iterdir()), "out-of-root recalculate submit wrote outside root")
        finally:
            aio_mod._copy_upload_to_path = original_copy_upload_to_path
            routes_mod._resolve_path = original_resolve_path
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_submit_cleanup_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        original_unlink_with_retries = routes_mod._unlink_with_retries
        original_rmtree_with_retries = routes_mod._rmtree_with_retries
        unlink_calls: list[Path] = []
        rmtree_calls: list[Path] = []

        async def recording_unlink(path: Path, delays_s: tuple[float, ...]) -> bool:
            unlink_calls.append(path)
            path.unlink(missing_ok=True)
            return True

        async def recording_rmtree(path: Path, delays_s: tuple[float, ...]) -> bool:
            rmtree_calls.append(path)
            shutil.rmtree(path)
            return True

        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        routes_mod.job_manager = _BusyManager()
        routes_mod._unlink_with_retries = recording_unlink
        routes_mod._rmtree_with_retries = recording_rmtree
        try:
            reward_response = await main_mod.reward_submit(thread_dir="thread_1", file=_upload())
            _assert(reward_response.status_code == 200, "reward busy response status changed")
            _assert(len(unlink_calls) == 1, f"reward rejected upload cleanup was not retried: {unlink_calls}")
            _assert(not unlink_calls[0].exists(), f"reward rejected upload still exists: {unlink_calls[0]}")

            recalc_response = await main_mod.recalculate_submit(file=_upload())
            _assert(recalc_response.status_code == 200, "recalculate busy response status changed")
            _assert(len(rmtree_calls) == 1, f"recalculate rejected dir cleanup was not retried: {rmtree_calls}")
            _assert(not rmtree_calls[0].exists(), f"recalculate rejected dir still exists: {rmtree_calls[0]}")
        finally:
            routes_mod.job_manager = original_manager
            routes_mod._unlink_with_retries = original_unlink_with_retries
            routes_mod._rmtree_with_retries = original_rmtree_with_retries
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_submit_cleanup_cancel_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        original_unlink_with_retries = routes_mod._unlink_with_retries
        original_rmtree_with_retries = routes_mod._rmtree_with_retries
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleaned_paths: list[Path] = []

        async def blocking_unlink(path: Path, delays_s: tuple[float, ...]) -> bool:
            cleanup_started.set()
            await cleanup_release.wait()
            path.unlink(missing_ok=True)
            cleaned_paths.append(path)
            return True

        async def blocking_rmtree(path: Path, delays_s: tuple[float, ...]) -> bool:
            cleanup_started.set()
            await cleanup_release.wait()
            shutil.rmtree(path)
            cleaned_paths.append(path)
            return True

        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        routes_mod.job_manager = _BusyManager()
        try:
            routes_mod._unlink_with_retries = blocking_unlink
            reward_task = asyncio.create_task(main_mod.reward_submit(thread_dir="thread_1", file=_upload()))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
            reward_task.cancel()
            await asyncio.sleep(0)
            reward_task.cancel()
            cleanup_release.set()
            try:
                await reward_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("double-cancelled reward cleanup did not raise CancelledError")
            _assert(cleaned_paths and not cleaned_paths[-1].exists(), "double-cancelled reward cleanup did not finish")

            cleanup_started = asyncio.Event()
            cleanup_release = asyncio.Event()
            routes_mod._rmtree_with_retries = blocking_rmtree
            recalc_task = asyncio.create_task(main_mod.recalculate_submit(file=_upload()))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
            recalc_task.cancel()
            await asyncio.sleep(0)
            recalc_task.cancel()
            cleanup_release.set()
            try:
                await recalc_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("double-cancelled recalculate cleanup did not raise CancelledError")
            _assert(cleaned_paths and not cleaned_paths[-1].exists(), "double-cancelled recalc cleanup did not finish")
        finally:
            cleanup_release.set()
            routes_mod.job_manager = original_manager
            routes_mod._unlink_with_retries = original_unlink_with_retries
            routes_mod._rmtree_with_retries = original_rmtree_with_retries
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_submit_cancel_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        manager = _DelayedAcceptManager()
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = manager
        try:
            task = asyncio.create_task(main_mod.reward_submit(thread_dir="thread_1", file=_upload()))
            await manager.started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            manager.release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled reward submit did not raise CancelledError")

            _assert(manager.job is not None, "reward submit did not create a job")
            _assert(
                Path(manager.job.proc_file).exists(),
                "accepted reward upload was deleted after request cancellation",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_copy_cancel_reward_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        sample_dir.mkdir(parents=True)
        (sample_dir / "instruction.json").write_text(
            json.dumps({"answer_position": "Sheet1!A1"}),
            encoding="utf-8",
        )
        (sample_dir / "target.xlsx").write_bytes(b"placeholder")

        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        original_manager = routes_mod.job_manager
        original_copy_upload = aio_mod._copy_upload_to_path
        blocking_copy = _BlockingCopy()
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        routes_mod.job_manager = _FakeManager()
        aio_mod._copy_upload_to_path = blocking_copy
        try:
            task = asyncio.create_task(main_mod.reward_submit(thread_dir="thread_1", file=_upload()))
            started = await asyncio.to_thread(blocking_copy.started.wait, 5.0)
            _assert(started, "reward upload copy did not start")
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            blocking_copy.release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled reward upload copy did not raise CancelledError")

            job_dir = sample_dir / ".async_reward_jobs"
            leaked_files = list(job_dir.glob("*.xlsx")) if job_dir.exists() else []
            _assert(not leaked_files, f"cancelled reward upload left partial files: {leaked_files}")
        finally:
            blocking_copy.release.set()
            routes_mod.job_manager = original_manager
            aio_mod._copy_upload_to_path = original_copy_upload
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    with temporary_directory(prefix="async_reward_api_copy_cancel_recalc_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_jobs"

        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        original_copy_upload = aio_mod._copy_upload_to_path
        blocking_copy = _BlockingCopy()
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        routes_mod.job_manager = _FakeManager()
        aio_mod._copy_upload_to_path = blocking_copy
        try:
            task = asyncio.create_task(main_mod.recalculate_submit(file=_upload()))
            started = await asyncio.to_thread(blocking_copy.started.wait, 5.0)
            _assert(started, "recalculate upload copy did not start")
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            blocking_copy.release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled recalculate upload copy did not raise CancelledError")

            leaked_entries = list(recalc_root.iterdir()) if recalc_root.exists() else []
            _assert(not leaked_entries, f"cancelled recalculate upload left job dirs: {leaked_entries}")
        finally:
            blocking_copy.release.set()
            routes_mod.job_manager = original_manager
            aio_mod._copy_upload_to_path = original_copy_upload
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_cancel_accept_") as tmp:
        tmp_path = Path(tmp)
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        manager = _DelayedAcceptManager()
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        routes_mod.job_manager = manager
        try:
            task = asyncio.create_task(main_mod.recalculate_submit(file=_upload()))
            await manager.started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            manager.release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled accepted recalculate submit did not raise CancelledError")

            _assert(manager.job is not None, "recalculate submit did not create a job")
            _assert(
                Path(manager.job.proc_file).exists(),
                "accepted recalculate upload was deleted after request cancellation",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_cancel_reject_") as tmp:
        tmp_path = Path(tmp)
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        manager = _DelayedAcceptManager(accepted=False)
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(tmp_path / "recalc_jobs")
        routes_mod.job_manager = manager
        try:
            task = asyncio.create_task(main_mod.recalculate_submit(file=_upload()))
            await manager.started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            manager.release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled rejected recalculate submit did not raise CancelledError")

            _assert(manager.job is not None, "recalculate reject path did not create a job")
            _assert(
                not Path(manager.job.proc_file).exists(),
                "rejected recalculate upload survived request cancellation",
            )
            _assert(
                not Path(manager.job.proc_file).parent.exists(),
                "rejected recalculate job directory survived request cancellation",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_root_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        outside_file = tmp_path / "outside.xlsx"
        outside_job_id = "44444444444444444444444444444444"
        recalc_root.mkdir()
        outside_file.write_bytes(b"placeholder")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        invalid_result_manager = _SnapshotManager(
            main_mod.JobSnapshot(
                job_id=outside_job_id,
                proc_file=outside_file,
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
                msg="",
            )
        )
        routes_mod.job_manager = invalid_result_manager
        try:
            result_response = await main_mod.recalculate_result(outside_job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": outside_job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "invalid result path",
                },
                f"unexpected out-of-root result payload: {result_payload}",
            )
            _assert(
                invalid_result_manager.mark_result_error_calls
                == [
                    {
                        "job_id": outside_job_id,
                        "kind": main_mod.JobKind.RECALCULATE,
                        "msg": "invalid result path",
                    }
                ],
                f"invalid result was not persisted: {invalid_result_manager.mark_result_error_calls}",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_shape_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        invalid_file = recalc_root / "important" / "workbook.xlsx"
        invalid_shape_job_id = "33333333333333333333333333333333"
        invalid_file.parent.mkdir(parents=True)
        invalid_file.write_bytes(b"placeholder")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        invalid_shape_manager = _SnapshotManager(
            main_mod.JobSnapshot(
                job_id=invalid_shape_job_id,
                proc_file=invalid_file,
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
                msg="",
            )
        )
        routes_mod.job_manager = invalid_shape_manager
        try:
            result_response = await main_mod.recalculate_result(invalid_shape_job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": invalid_shape_job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "invalid result path",
                },
                f"unexpected invalid-shape result payload: {result_payload}",
            )
            _assert(
                invalid_shape_manager.mark_result_error_calls
                and invalid_shape_manager.mark_result_error_calls[-1]["msg"] == "invalid result path",
                f"invalid-shape result was not persisted: {invalid_shape_manager.mark_result_error_calls}",
            )
            _assert(invalid_file.exists(), "invalid-shape recalculate result file was modified")
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_malformed_proc_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        malformed_proc_job_id = "55555555555555555555555555555555"
        malformed_proc_file = Path(
            str(recalc_root / malformed_proc_job_id / "workbook.xlsx") + "\x00"
        )
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        malformed_proc_manager = _SnapshotManager(
            main_mod.JobSnapshot(
                job_id=malformed_proc_job_id,
                proc_file=malformed_proc_file,
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
                msg="",
            )
        )
        routes_mod.job_manager = malformed_proc_manager
        try:
            result_response = await main_mod.recalculate_result(malformed_proc_job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": malformed_proc_job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "invalid result path",
                },
                f"unexpected malformed-proc result payload: {result_payload}",
            )
            _assert(
                malformed_proc_manager.mark_result_error_calls
                and malformed_proc_manager.mark_result_error_calls[-1]["msg"] == "invalid result path",
                f"malformed-proc result was not persisted: {malformed_proc_manager.mark_result_error_calls}",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_expired_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        job_id = "11111111111111111111111111111111"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        test_manager = None
        try:
            store = main_mod.SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            job = main_mod.JobRecord(
                job_id=job_id,
                thread_dir="recalculate",
                gt_file=expected_file,
                proc_file=expected_file,
                answer_position="",
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
            )
            _assert(store.enqueue(job, max_queue_size=10), "expired recalculate result job was not accepted")
            test_manager = main_mod.RewardJobManager(
                store=store,
                platform=main_mod.Platform.WINDOWS,
            )
            routes_mod.job_manager = test_manager
            result_response = await main_mod.recalculate_result(job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "result expired",
                },
                f"unexpected expired-result payload: {result_payload}",
            )
            status_response = await main_mod.recalculate_status(job_id)
            status_payload = json.loads(status_response.body.decode("utf-8"))
            _assert(
                status_payload["status"] == main_mod.JobStatus.ERROR.value,
                f"expired-result status was not persisted: {status_payload}",
            )
        finally:
            if test_manager is not None:
                await test_manager.shutdown()
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_expire_mark_fail_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        job_id = "66666666666666666666666666666666"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        routes_mod.job_manager = _MarkExpiredFailManager(
            main_mod.JobSnapshot(
                job_id=job_id,
                proc_file=expected_file,
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
                msg="",
            )
        )
        try:
            result_response = await main_mod.recalculate_result(job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "result expired",
                },
                f"unexpected mark-fail expired-result payload: {result_payload}",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_expire_mark_noop_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        job_id = "88888888888888888888888888888888"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        routes_mod.job_manager = _MarkExpiredNoopManager(
            main_mod.JobSnapshot(
                job_id=job_id,
                proc_file=expected_file,
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
                msg="",
            )
        )
        try:
            result_response = await main_mod.recalculate_result(job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "result expired",
                },
                f"unexpected noop expired-result payload: {result_payload}",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_expire_mark_race_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        job_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        routes_mod.job_manager = _MarkExpiredRaceManager(
            main_mod.JobSnapshot(
                job_id=job_id,
                proc_file=expected_file,
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
                msg="",
            )
        )
        try:
            result_response = await main_mod.recalculate_result(job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "result expired",
                },
                f"unexpected raced expired-result payload: {result_payload}",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_expire_mark_deleted_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        job_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        routes_mod.job_manager = _MarkExpiredDeletedManager(
            main_mod.JobSnapshot(
                job_id=job_id,
                proc_file=expected_file,
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
                msg="",
            )
        )
        try:
            await _expect_http_error(
                "deleted expired recalculate result",
                main_mod.recalculate_result(job_id),
                status_code=404,
                detail="job not found",
            )
        finally:
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_locked_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        job_id = "22222222222222222222222222222222"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        expected_file.write_bytes(b"placeholder")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        original_open_binary = routes_mod._open_binary

        def raise_permission_error(path: Path):
            raise PermissionError(f"locked: {path}")

        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(recalc_root)
        test_manager = None
        try:
            store = main_mod.SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            job = main_mod.JobRecord(
                job_id=job_id,
                thread_dir="recalculate",
                gt_file=expected_file,
                proc_file=expected_file,
                answer_position="",
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
            )
            _assert(store.enqueue(job, max_queue_size=10), "locked recalculate result job was not accepted")
            test_manager = main_mod.RewardJobManager(
                store=store,
                platform=main_mod.Platform.WINDOWS,
            )
            routes_mod.job_manager = test_manager
            routes_mod._open_binary = raise_permission_error
            await _expect_http_error(
                "locked recalculate result",
                main_mod.recalculate_result(job_id),
                status_code=503,
                detail="result temporarily unavailable",
            )
            snapshot = store.get_snapshot(job_id)
            _assert(snapshot is not None, "locked-result row disappeared")
            _assert(
                snapshot.status is main_mod.JobStatus.DONE,
                f"locked-result open failure mutated job state: {snapshot}",
            )
        finally:
            routes_mod._open_binary = original_open_binary
            if test_manager is not None:
                await test_manager.shutdown()
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_stored_root_drift_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        changed_root = tmp_path / "changed_recalc_root"
        job_id = "77777777777777777777777777777777"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        expected_file.write_bytes(b"placeholder")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(changed_root)
        test_manager = None
        try:
            changed_valid_job_id = "88888888888888888888888888888888"
            changed_valid_file = changed_root / changed_valid_job_id / "workbook.xlsx"
            changed_valid_file.parent.mkdir(parents=True)
            changed_valid_file.write_bytes(b"valid under current root")
            store = main_mod.SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            job = main_mod.JobRecord(
                job_id=job_id,
                thread_dir="recalculate",
                gt_file=recalc_root,
                proc_file=expected_file,
                answer_position="",
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
            )
            valid_job = main_mod.JobRecord(
                job_id=changed_valid_job_id,
                thread_dir="recalculate",
                gt_file=changed_root,
                proc_file=changed_valid_file,
                answer_position="",
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
            )
            _assert(
                not main_mod._persisted_recalculate_job_paths_are_safe(job),
                "stored-root-drift recalculate path still passed the current-root anchor",
            )
            _assert(
                main_mod._persisted_recalculate_job_paths_are_safe(valid_job),
                "current-root recalculate path was rejected by the anchor",
            )
            _assert(store.enqueue(job, max_queue_size=10), "root-drift recalculate result job was not accepted")
            test_manager = main_mod.RewardJobManager(
                store=store,
                platform=main_mod.Platform.WINDOWS,
            )
            routes_mod.job_manager = test_manager
            result_response = await main_mod.recalculate_result(job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "invalid result path",
                },
                f"stored-root-drift recalculate result changed: {result_payload}",
            )
            status_response = await main_mod.recalculate_status(job_id)
            status_payload = json.loads(status_response.body.decode("utf-8"))
            _assert(
                status_payload["status"] == main_mod.JobStatus.ERROR.value,
                f"stored-root-drift status was not persisted as error: {status_payload}",
            )
            _assert(expected_file.exists(), "stored-root-drift result path was modified")
            snapshot = store.get_snapshot(job_id)
            _assert(snapshot is not None and snapshot.status is main_mod.JobStatus.ERROR, "root-drift row was not persisted as error")
        finally:
            if test_manager is not None:
                await test_manager.shutdown()
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    with temporary_directory(prefix="async_reward_api_recalc_result_legacy_root_drift_") as tmp:
        tmp_path = Path(tmp)
        recalc_root = tmp_path / "recalc_root"
        changed_root = tmp_path / "changed_recalc_root"
        job_id = "99999999999999999999999999999999"
        expected_file = recalc_root / job_id / "workbook.xlsx"
        expected_file.parent.mkdir(parents=True)
        expected_file.write_bytes(b"placeholder")
        original_recalc_root = os.environ.get("REWARD_API_RECALC_JOB_ROOT")
        original_manager = routes_mod.job_manager
        os.environ["REWARD_API_RECALC_JOB_ROOT"] = str(changed_root)
        test_manager = None
        try:
            store = main_mod.SqliteJobStore(tmp_path / "jobs.sqlite3")
            store.init()
            job = main_mod.JobRecord(
                job_id=job_id,
                thread_dir="recalculate",
                gt_file=expected_file,
                proc_file=expected_file,
                answer_position="",
                kind=main_mod.JobKind.RECALCULATE,
                status=main_mod.JobStatus.DONE,
                created_at_s=1.0,
                started_at_s=1.0,
                finished_at_s=1.0,
                reward=0.0,
            )
            _assert(
                not main_mod._persisted_recalculate_job_paths_are_safe(job),
                "legacy root-drift recalculate path still passed the current-root anchor",
            )
            _assert(store.enqueue(job, max_queue_size=10), "legacy-root recalculate result job was not accepted")
            test_manager = main_mod.RewardJobManager(
                store=store,
                platform=main_mod.Platform.WINDOWS,
            )
            routes_mod.job_manager = test_manager
            result_response = await main_mod.recalculate_result(job_id)
            result_payload = json.loads(result_response.body.decode("utf-8"))
            _assert(
                result_payload == {
                    "job_id": job_id,
                    "status": main_mod.JobStatus.ERROR.value,
                    "msg": "invalid result path",
                },
                f"legacy root-drift recalculate result changed: {result_payload}",
            )
        finally:
            if test_manager is not None:
                await test_manager.shutdown()
            routes_mod.job_manager = original_manager
            if original_recalc_root is None:
                os.environ.pop("REWARD_API_RECALC_JOB_ROOT", None)
            else:
                os.environ["REWARD_API_RECALC_JOB_ROOT"] = original_recalc_root

    original_manager = routes_mod.job_manager
    routes_mod.job_manager = _SnapshotManager(
        main_mod.JobSnapshot(
            job_id="wait-validation",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.REWARD,
            status=main_mod.JobStatus.QUEUED,
            created_at_s=1.0,
            started_at_s=None,
            finished_at_s=None,
            reward=None,
            msg="",
        )
    )
    try:
        for wait_s in (float("nan"), float("inf"), -1.0):
            await _expect_http_error(
                f"invalid reward wait_s {wait_s!r}",
                main_mod.reward_result("wait-validation", wait_s=wait_s),
                status_code=400,
                detail="wait_s must be finite and non-negative",
            )
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    routes_mod.job_manager = _DisappearRefreshManager(
        main_mod.JobSnapshot(
            job_id="wait-refresh-disappear",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.REWARD,
            status=main_mod.JobStatus.RUNNING,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=None,
            reward=None,
            msg="",
        )
    )
    try:
        await _expect_http_error(
            "reward result wait refresh disappearance",
            main_mod.reward_result("wait-refresh-disappear", wait_s=0.01),
            status_code=404,
            detail="job not found",
        )
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    routes_mod.job_manager = _FailSnapshotManager()
    try:
        for label, coro in (
            ("reward status snapshot failure", main_mod.reward_status("snapshot-fail")),
            ("reward result snapshot failure", main_mod.reward_result("snapshot-fail")),
            ("recalculate status snapshot failure", main_mod.recalculate_status("snapshot-fail")),
            ("recalculate result snapshot failure", main_mod.recalculate_result("snapshot-fail")),
        ):
            await _expect_http_error(
                label,
                coro,
                status_code=503,
                detail="job store temporarily unavailable",
            )
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    routes_mod.job_manager = _FailRefreshManager(
        main_mod.JobSnapshot(
            job_id="wait-refresh-fail",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.REWARD,
            status=main_mod.JobStatus.QUEUED,
            created_at_s=1.0,
            started_at_s=None,
            finished_at_s=None,
            reward=None,
            msg="",
        )
    )
    try:
        await _expect_http_error(
            "reward result wait refresh snapshot failure",
            main_mod.reward_result("wait-refresh-fail", wait_s=0.01),
            status_code=503,
            detail="job store temporarily unavailable",
        )
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    routes_mod.job_manager = _StatsFailManager()
    try:
        response = await main_mod.health()
        payload = json.loads(response.body.decode("utf-8"))
        _assert(response.status_code == 503, f"health stats failure status changed: {response.status_code}")
        _assert(
            payload == {"status": "degraded", "ready": False, "error": "stats unavailable"},
            f"unexpected health failure payload: {payload}",
        )
    finally:
        routes_mod.job_manager = original_manager

    with temporary_directory(prefix="async_reward_api_reward_numeric_leak_") as tmp:
        tmp_path = Path(tmp)
        store = main_mod.SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        job_id = "persisted-numeric-leak"
        job = main_mod.JobRecord(
            job_id=job_id,
            thread_dir="thread_1",
            gt_file=tmp_path / "target.xlsx",
            proc_file=tmp_path / "output.xlsx",
            answer_position="Sheet1!A1",
            status=main_mod.JobStatus.DONE,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=1.0,
            reward=1.0,
        )
        _assert(store.enqueue(job, max_queue_size=10), "numeric leak job was not accepted")
        conn = sqlite3.connect(store.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET created_at_s = ? WHERE job_id = ?;",
                    ("C:\\secret\\jobs.sqlite3", job_id),
                )
        finally:
            conn.close()
        original_manager = routes_mod.job_manager
        test_manager = main_mod.RewardJobManager(
            store=store,
            platform=main_mod.Platform.WINDOWS,
        )
        routes_mod.job_manager = test_manager
        try:
            response = await main_mod.reward_result(job_id)
            payload = json.loads(response.body.decode("utf-8"))
            _assert(payload["status"] == main_mod.JobStatus.ERROR.value, f"corrupt row status changed: {payload}")
            _assert(payload["reward"] == 0.0, f"corrupt row reward changed: {payload}")
            _assert("invalid persisted job row" in payload["msg"], f"corrupt row msg changed: {payload}")
            _assert("C:\\secret" not in payload["msg"], f"corrupt row leaked raw value: {payload}")
        finally:
            await test_manager.shutdown()
            routes_mod.job_manager = original_manager

    with temporary_directory(prefix="async_reward_api_reward_null_persist_") as tmp:
        tmp_path = Path(tmp)
        store = main_mod.SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        job_id = "persisted-null-reward"
        job = main_mod.JobRecord(
            job_id=job_id,
            thread_dir="thread_1",
            gt_file=tmp_path / "target.xlsx",
            proc_file=tmp_path / "output.xlsx",
            answer_position="Sheet1!A1",
            status=main_mod.JobStatus.DONE,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=1.0,
            reward=1.0,
        )
        _assert(store.enqueue(job, max_queue_size=10), "null reward job was not accepted")
        conn = sqlite3.connect(store.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET reward = NULL WHERE job_id = ?;",
                    (job_id,),
                )
        finally:
            conn.close()
        original_manager = routes_mod.job_manager
        test_manager = main_mod.RewardJobManager(
            store=store,
            platform=main_mod.Platform.WINDOWS,
        )
        routes_mod.job_manager = test_manager
        try:
            response = await main_mod.reward_result(job_id)
            payload = json.loads(response.body.decode("utf-8"))
            _assert(payload["status"] == main_mod.JobStatus.ERROR.value, f"null reward status changed: {payload}")
            _assert(payload["reward"] == 0.0, f"null reward value changed: {payload}")
            _assert("invalid persisted job row" in payload["msg"], f"null reward msg changed: {payload}")
            with closing(sqlite3.connect(store.db_path)) as raw_conn:
                raw_row = raw_conn.execute(
                    "SELECT status, reward, msg FROM jobs WHERE job_id = ?;",
                    (job_id,),
                ).fetchone()
            _assert(raw_row is not None, "null reward row disappeared")
            _assert(raw_row[0] == main_mod.JobStatus.ERROR.value, f"null reward row was not persisted: {raw_row}")
            _assert(raw_row[1] == 0.0, f"null reward row reward was not corrected: {raw_row}")
            _assert(raw_row[2] == "invalid persisted job row", f"null reward row msg changed: {raw_row}")
        finally:
            await test_manager.shutdown()
            routes_mod.job_manager = original_manager

    with temporary_directory(prefix="async_reward_api_reward_text_persist_") as tmp:
        tmp_path = Path(tmp)
        store = main_mod.SqliteJobStore(tmp_path / "jobs.sqlite3")
        store.init()
        job_id = "persisted-empty-proc-file"
        job = main_mod.JobRecord(
            job_id=job_id,
            thread_dir="thread_1",
            gt_file=tmp_path / "target.xlsx",
            proc_file=tmp_path / "output.xlsx",
            answer_position="Sheet1!A1",
            status=main_mod.JobStatus.DONE,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=1.0,
            reward=1.0,
        )
        _assert(store.enqueue(job, max_queue_size=10), "text-corrupt reward job was not accepted")
        conn = sqlite3.connect(store.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET proc_file = ? WHERE job_id = ?;",
                    ("", job_id),
                )
        finally:
            conn.close()
        original_manager = routes_mod.job_manager
        test_manager = main_mod.RewardJobManager(
            store=store,
            platform=main_mod.Platform.WINDOWS,
        )
        routes_mod.job_manager = test_manager
        try:
            response = await main_mod.reward_result(job_id)
            payload = json.loads(response.body.decode("utf-8"))
            _assert(payload["status"] == main_mod.JobStatus.ERROR.value, f"text corrupt status changed: {payload}")
            _assert(payload["reward"] == 0.0, f"text corrupt reward leaked stale value: {payload}")
            _assert("invalid persisted job row" in payload["msg"], f"text corrupt msg changed: {payload}")
            with closing(sqlite3.connect(store.db_path)) as raw_conn:
                raw_row = raw_conn.execute(
                    "SELECT status, reward, msg FROM jobs WHERE job_id = ?;",
                    (job_id,),
                ).fetchone()
            _assert(raw_row is not None, "text corrupt row disappeared")
            _assert(raw_row[0] == main_mod.JobStatus.ERROR.value, f"text corrupt row was not persisted: {raw_row}")
            _assert(raw_row[1] == 0.0, f"text corrupt row reward was not corrected: {raw_row}")
            _assert(raw_row[2] == "invalid persisted job row", f"text corrupt row msg changed: {raw_row}")
        finally:
            await test_manager.shutdown()
            routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    non_finite_manager = _SnapshotManager(
        main_mod.JobSnapshot(
            job_id="non-finite-reward",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.REWARD,
            status=main_mod.JobStatus.DONE,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=1.0,
            reward=float("inf"),
            msg="",
        )
    )
    routes_mod.job_manager = non_finite_manager
    try:
        response = await main_mod.reward_result("non-finite-reward")
        payload = json.loads(response.body.decode("utf-8"))
        _assert(
            payload == {
                "job_id": "non-finite-reward",
                "status": main_mod.JobStatus.ERROR.value,
                "reward": 0.0,
                "msg": "invalid persisted job row",
            },
            f"unexpected non-finite reward payload: {payload}",
        )
        _assert(
            non_finite_manager.mark_result_error_calls
            and non_finite_manager.mark_result_error_calls[-1]["msg"] == "invalid persisted job row",
            f"invalid reward was not persisted: {non_finite_manager.mark_result_error_calls}",
        )
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    path_msg = "open failed for C:\\secret dir\\book.xlsx " + ("x" * 600)
    routes_mod.job_manager = _SnapshotManager(
        main_mod.JobSnapshot(
            job_id="legacy-reward-msg",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.REWARD,
            status=main_mod.JobStatus.ERROR,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=1.0,
            reward=0.0,
            msg=path_msg,
        )
    )
    try:
        response = await main_mod.reward_result("legacy-reward-msg")
        payload = json.loads(response.body.decode("utf-8"))
        _assert("C:\\secret" not in payload["msg"], f"legacy reward msg leaked path: {payload}")
        _assert(len(payload["msg"]) <= 500, f"legacy reward msg was not capped: {len(payload['msg'])}")
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    routes_mod.job_manager = _SnapshotManager(
        main_mod.JobSnapshot(
            job_id="legacy-recalc-msg",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.RECALCULATE,
            status=main_mod.JobStatus.ERROR,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=1.0,
            reward=0.0,
            msg=path_msg,
        )
    )
    try:
        response = await main_mod.recalculate_result("legacy-recalc-msg")
        payload = json.loads(response.body.decode("utf-8"))
        _assert("C:\\secret" not in payload["msg"], f"legacy recalc msg leaked path: {payload}")
        _assert(len(payload["msg"]) <= 500, f"legacy recalc msg was not capped: {len(payload['msg'])}")
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    missing_reward_manager = _SnapshotManager(
        main_mod.JobSnapshot(
            job_id="missing-terminal-reward",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.REWARD,
            status=main_mod.JobStatus.DONE,
            created_at_s=1.0,
            started_at_s=1.0,
            finished_at_s=1.0,
            reward=None,
            msg="",
        )
    )
    routes_mod.job_manager = missing_reward_manager
    try:
        response = await main_mod.reward_result("missing-terminal-reward")
        payload = json.loads(response.body.decode("utf-8"))
        _assert(
            payload == {
                "job_id": "missing-terminal-reward",
                "status": main_mod.JobStatus.ERROR.value,
                "reward": 0.0,
                "msg": "invalid persisted job row",
            },
            f"unexpected missing terminal reward payload: {payload}",
        )
        _assert(
            missing_reward_manager.mark_result_error_calls
            and missing_reward_manager.mark_result_error_calls[-1]["msg"] == "invalid persisted job row",
            f"missing reward was not persisted: {missing_reward_manager.mark_result_error_calls}",
        )
    finally:
        routes_mod.job_manager = original_manager

    original_manager = routes_mod.job_manager
    routes_mod.job_manager = _SnapshotManager(
        main_mod.JobSnapshot(
            job_id="wait-validation-recalc",
            proc_file=Path("workbook.xlsx"),
            kind=main_mod.JobKind.RECALCULATE,
            status=main_mod.JobStatus.QUEUED,
            created_at_s=1.0,
            started_at_s=None,
            finished_at_s=None,
            reward=None,
            msg="",
        )
    )
    try:
        for wait_s in (float("nan"), float("inf"), -1.0):
            await _expect_http_error(
                f"invalid recalculate wait_s {wait_s!r}",
                main_mod.recalculate_result("wait-validation-recalc", wait_s=wait_s),
                status_code=400,
                detail="wait_s must be finite and non-negative",
            )
    finally:
        routes_mod.job_manager = original_manager

    print("OK: submit error paths look good")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
