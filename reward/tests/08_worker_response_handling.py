from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

from _tempdir import temporary_directory

os.environ["REWARD_API_ALLOW_UNSUPPORTED_HOST_FOR_TESTS"] = "1"
os.environ["REWARD_API_PLATFORM"] = "windows"

from async_reward_api import manager as manager_mod  # noqa: E402
from async_reward_api import recalc_on_windows as recalc_mod  # noqa: E402
from async_reward_api import worker as worker_mod  # noqa: E402
from async_reward_api import excel_pool as excel_pool_mod  # noqa: E402
from async_reward_api import windows_process as windows_process_mod  # noqa: E402
from async_reward_api import excel_worker_server as excel_worker_server_mod  # noqa: E402
from async_reward_api.excel_worker_server import _WindowsExcelSession  # noqa: E402
from async_reward_api.excel_com import FatalExcelSessionError  # noqa: E402
from async_reward_api.excel_pool import (  # noqa: E402
    ExcelWorkerPool,
    ExcelWorkerProcess,
    WorkerDiedError,
    WorkerProtocolError,
    WorkerResult,
    _coerce_ready_pid,
)
from async_reward_api.messages import public_worker_message  # noqa: E402
from async_reward_api.models import JobRecord, JobStatus  # noqa: E402
from async_reward_api.platform import Platform  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        rows = [
            row.strip()
            for row in (completed.stdout or "").splitlines()
            if row.strip() and not row.strip().upper().startswith("INFO:")
        ]
        return bool(rows)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def _with_fake_worker(fake_worker, coro):
    original = manager_mod._run_worker_subprocess

    async def fake_worker_async(**kwargs):
        result = fake_worker(**kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    manager_mod._run_worker_subprocess = fake_worker_async
    try:
        return await coro()
    finally:
        manager_mod._run_worker_subprocess = original


class _FakeStdin:
    def write(self, _: str) -> None:
        return None

    def flush(self) -> None:
        return None


class _FakeProc:
    stdin = _FakeStdin()

    def poll(self):
        return None


class _RecordingStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> None:
        self.writes.append(text)

    def flush(self) -> None:
        return None


class _FakeStartedProc:
    stdout = io.StringIO("")
    stderr = io.StringIO("")
    pid = 123456

    def __init__(self) -> None:
        self.stdin = _RecordingStdin()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class _BlockingWriteStdin:
    def __init__(self) -> None:
        self.write_started = threading.Event()
        self.write_release = threading.Event()
        self.writes: list[str] = []

    def write(self, text: str) -> None:
        self.write_started.set()
        self.write_release.wait(timeout=5.0)
        self.writes.append(text)

    def flush(self) -> None:
        return None


class _BlockingWriteProc(_FakeStartedProc):
    def __init__(self) -> None:
        super().__init__()
        self.stdin = _BlockingWriteStdin()


class _BlockingWaitProc(_FakeStartedProc):
    def __init__(self) -> None:
        super().__init__()
        self.wait_started = threading.Event()
        self.wait_release = threading.Event()

    def wait(self, timeout=None):
        self.wait_started.set()
        self.wait_release.wait(timeout=5.0)
        self.returncode = 0
        return 0


class _ClosedLoop:
    def call_soon_threadsafe(self, *args, **kwargs):
        raise RuntimeError("Event loop is closed")


class _ProtocolErrorWorker:
    jobs_run = 0
    excel_pid = None
    is_running = True

    def __init__(self, msg: str) -> None:
        self._msg = msg
        self.shutdown_called = False

    async def run_job(self, **kwargs):
        raise WorkerProtocolError(self._msg)

    async def run_recalc(self, **kwargs):
        raise WorkerProtocolError(self._msg)

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_called = True


class _DiedPoolWorker:
    jobs_run = 0
    excel_pid = None
    is_running = True

    def __init__(self, msg: str) -> None:
        self._msg = msg
        self.shutdown_called = False

    async def run_job(self, **kwargs):
        raise WorkerDiedError(self._msg)

    async def run_recalc(self, **kwargs):
        raise WorkerDiedError(self._msg)

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_called = True
        self.is_running = False


class _HangingPoolWorker:
    jobs_run = 0
    excel_pid = None
    is_running = True

    async def run_job(self, **kwargs):
        await asyncio.Event().wait()

    async def run_recalc(self, **kwargs):
        await asyncio.Event().wait()

    async def shutdown(self, *, force: bool = False) -> None:
        self.is_running = False


class _ReplacementShutdownWorker:
    jobs_run = 0
    excel_pid = None
    is_running = True

    def __init__(self) -> None:
        self.shutdown_started = asyncio.Event()
        self.shutdown_release = asyncio.Event()

    async def run_job(self, **kwargs):
        raise asyncio.TimeoutError()

    async def run_recalc(self, **kwargs):
        raise asyncio.TimeoutError()

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_started.set()
        await self.shutdown_release.wait()
        self.is_running = False


class _SuccessfulPoolWorker:
    is_running = True

    def __init__(self, *, msg: str = "", excel_pid: int | None = 12345) -> None:
        self.jobs_run = 1
        self.excel_pid = excel_pid
        self._msg = msg

    async def run_job(self, **kwargs):
        return WorkerResult(ok=True, reward=1.0, msg=self._msg)

    async def run_recalc(self, **kwargs):
        return WorkerResult(ok=True, reward=0.0, msg=self._msg)

    async def shutdown(self, *, force: bool = False) -> None:
        self.is_running = False


class _FailingPoolWorker:
    is_running = True
    excel_pid = None
    jobs_run = 1

    def __init__(self, *, msg: str = "bad workbook") -> None:
        self._msg = msg
        self.shutdown_called = False

    async def run_job(self, **kwargs):
        return WorkerResult(ok=False, reward=0.0, msg=self._msg)

    async def run_recalc(self, **kwargs):
        return WorkerResult(ok=False, reward=0.0, msg=self._msg)

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_called = True
        self.is_running = False


class _ReplacementPoolWorker:
    jobs_run = 0
    excel_pid = None

    def __init__(self, *args, **kwargs) -> None:
        self.is_running = True
        self.start_called = False
        self.shutdown_called = False

    async def start(self) -> None:
        self.start_called = True

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_called = True
        self.is_running = False


class _IdlePoolWorker:
    jobs_run = 0
    excel_pid = None

    def __init__(self, *, is_running: bool) -> None:
        self.is_running = is_running


class _BlockingShutdownPoolWorker:
    jobs_run = 0
    excel_pid = None
    is_running = True

    def __init__(self, *, block: bool) -> None:
        self.block = block
        self.shutdown_calls: list[bool] = []
        self.shutdown_cancelled = False
        self.shutdown_started = asyncio.Event()
        self.shutdown_release = asyncio.Event()

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_calls.append(force)
        self.shutdown_started.set()
        try:
            if self.block:
                await self.shutdown_release.wait()
        except asyncio.CancelledError:
            self.shutdown_cancelled = True
            self.is_running = False
            raise
        else:
            self.is_running = False


class _RepeatedCancelShutdownPoolWorker:
    jobs_run = 0
    excel_pid = None

    def __init__(self) -> None:
        self.is_running = True
        self.shutdown_started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_completed = False
        self.cleanup_interrupted = False

    async def shutdown(self, *, force: bool = False) -> None:
        self.shutdown_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleanup_started.set()
            try:
                await self.cleanup_release.wait()
            except asyncio.CancelledError:
                self.cleanup_interrupted = True
                raise
            self.cleanup_completed = True
            self.is_running = False
            raise


async def _default_fake_worker_is_healthy(self) -> bool:
    return bool(self.is_running)


for _worker_cls in (
    _ProtocolErrorWorker,
    _DiedPoolWorker,
    _HangingPoolWorker,
    _ReplacementShutdownWorker,
    _SuccessfulPoolWorker,
    _FailingPoolWorker,
    _ReplacementPoolWorker,
    _IdlePoolWorker,
    _BlockingShutdownPoolWorker,
    _RepeatedCancelShutdownPoolWorker,
):
    _worker_cls.is_healthy = _default_fake_worker_is_healthy


class _DelayedHealthWorker:
    is_running = True

    def __init__(self, *, delay_s: float, healthy: bool = True, fail: bool = False) -> None:
        self.delay_s = delay_s
        self.healthy = healthy
        self.fail = fail

    async def is_healthy(self) -> bool:
        await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("health probe failed")
        return self.healthy


class _BlockingHealthWorker:
    is_running = True
    jobs_run = 0
    excel_pid = None

    def __init__(self) -> None:
        self.health_started = asyncio.Event()
        self.health_release = asyncio.Event()

    async def is_healthy(self) -> bool:
        self.health_started.set()
        await self.health_release.wait()
        return True


class _CountingHealthWorker:
    is_running = True
    jobs_run = 0
    excel_pid = None

    def __init__(self, *, healthy: bool) -> None:
        self.healthy = healthy
        self.health_calls = 0

    async def is_healthy(self) -> bool:
        self.health_calls += 1
        return self.healthy


class _CloseFailWorkbook:
    def Save(self) -> None:
        return None

    def Close(self, *, SaveChanges: bool) -> None:
        raise RuntimeError("close failed")


class _FakeWorkbooks:
    def __init__(self, workbook, *, expected_filename: str | None = None) -> None:
        self._workbook = workbook
        self._expected_filename = expected_filename

    def Open(self, **kwargs):
        if self._expected_filename is not None:
            _assert(
                os.path.abspath(str(kwargs.get("Filename"))) == os.path.abspath(self._expected_filename),
                f"fake Excel opened unexpected filename: {kwargs}",
            )
        return self._workbook


class _FakeExcelApp:
    def __init__(self, workbook, *, expected_filename: str | None = None) -> None:
        self.Workbooks = _FakeWorkbooks(workbook, expected_filename=expected_filename)
        self.Calculation = -4105

    def CalculateFullRebuild(self) -> None:
        return None

    def CalculateUntilAsyncQueriesDone(self) -> None:
        return None


class _OpenParityWorkbook:
    def __init__(self, *, full_name: str | None = None, on_close=None) -> None:
        self.close_save_changes: list[bool] = []
        self.save_calls = 0
        self._on_close = on_close
        if full_name is not None:
            self.FullName = full_name

    def Save(self) -> None:
        self.save_calls += 1
        return None

    def Close(self, *, SaveChanges: bool) -> None:
        self.close_save_changes.append(SaveChanges)
        if self._on_close is not None:
            self._on_close(self)
        return None


class _CloseFailOpenParityWorkbook(_OpenParityWorkbook):
    def Close(self, *, SaveChanges: bool) -> None:
        self.close_save_changes.append(SaveChanges)
        raise RuntimeError("close failed")


class _SaveFailOpenParityWorkbook(_OpenParityWorkbook):
    def __init__(self, *, save_error: str = "save failed", **kwargs) -> None:
        super().__init__(**kwargs)
        self._save_error = save_error

    def Save(self) -> None:
        self.save_calls += 1
        raise RuntimeError(self._save_error)


class _SaveAndCloseFailOpenParityWorkbook(_SaveFailOpenParityWorkbook):
    def __init__(self, *, close_error: str = "close failed", **kwargs) -> None:
        super().__init__(**kwargs)
        self._close_error = close_error

    def Close(self, *, SaveChanges: bool) -> None:
        self.close_save_changes.append(SaveChanges)
        raise RuntimeError(self._close_error)


class _OpenParityWorkbooks:
    def __init__(self, *, expected_filename: str | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.workbook = _OpenParityWorkbook()
        self._expected_filename = expected_filename

    def Open(self, **kwargs):
        self.calls.append(kwargs)
        if self._expected_filename is not None:
            _assert(
                os.path.abspath(str(kwargs.get("Filename"))) == os.path.abspath(self._expected_filename),
                f"open parity fake opened unexpected filename: {kwargs}",
            )
        if kwargs.get("CorruptLoad") != 1:
            raise RuntimeError("expected CorruptLoad open attempt")
        return self.workbook


class _NoneReturningOpenWorkbooks:
    def __init__(
        self,
        *,
        full_name: str,
        workbook_full_name: str | None = None,
        preexisting_full_name: str | None = os.path.abspath("preexisting.xlsx"),
        extra_full_names: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.full_name = full_name
        if workbook_full_name is None:
            workbook_full_name = full_name
        self.preexisting_workbook = (
            _OpenParityWorkbook(full_name=preexisting_full_name)
            if preexisting_full_name is not None
            else None
        )
        self.extra_workbooks = [
            _OpenParityWorkbook(full_name=extra_full_name, on_close=self._mark_closed)
            for extra_full_name in extra_full_names
        ]
        self.workbook = _OpenParityWorkbook(full_name=workbook_full_name, on_close=self._mark_closed)
        self._opened = False
        self._open_new_workbooks = []
        self.active_workbook = None

    @property
    def Count(self) -> int:
        preexisting_count = 1 if self.preexisting_workbook is not None else 0
        if not self._opened:
            return preexisting_count
        return preexisting_count + len(self._open_new_workbooks)

    def __call__(self, index: int):
        if index == 1 and self.preexisting_workbook is not None:
            return self.preexisting_workbook
        offset = 2 if self.preexisting_workbook is not None else 1
        if self._opened:
            if offset <= index <= len(self._open_new_workbooks) + offset - 1:
                return self._open_new_workbooks[index - offset]
        raise RuntimeError("workbook index not open")

    def Open(self, **kwargs):
        self.calls.append(kwargs)
        if os.path.abspath(str(kwargs.get("Filename"))) != os.path.abspath(str(self.full_name)):
            raise RuntimeError("wrong Filename open attempt")
        self._opened = True
        self._open_new_workbooks = [*self.extra_workbooks, self.workbook]
        return None

    def _mark_closed(self, workbook) -> None:
        self._open_new_workbooks = [
            open_workbook
            for open_workbook in self._open_new_workbooks
            if open_workbook is not workbook
        ]


class _StaleActiveOpenWorkbooks:
    def __init__(self, *, stale_full_name: str, expected_filename: str | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.workbook = _OpenParityWorkbook(full_name=stale_full_name)
        self._expected_filename = expected_filename

    @property
    def Count(self) -> int:
        return 1

    def __call__(self, index: int):
        if index == 1:
            return self.workbook
        raise RuntimeError("workbook index not open")

    def Open(self, **kwargs):
        self.calls.append(kwargs)
        if self._expected_filename is not None:
            _assert(
                os.path.abspath(str(kwargs.get("Filename"))) == os.path.abspath(self._expected_filename),
                f"stale active fake opened unexpected filename: {kwargs}",
            )
        return None


class _CountFailOpenWorkbooks(_NoneReturningOpenWorkbooks):
    @property
    def Count(self) -> int:
        raise RuntimeError("count failed")


class _CurrentCountFailOpenWorkbooks(_NoneReturningOpenWorkbooks):
    @property
    def Count(self) -> int:
        if self._opened:
            raise RuntimeError("current count failed")
        return super().Count


class _IndexFailOpenWorkbooks(_NoneReturningOpenWorkbooks):
    def __call__(self, index: int):
        if index == 1:
            return self.preexisting_workbook
        if self._opened and index == 2:
            raise RuntimeError("index failed")
        return super().__call__(index)


class _OpenParityExcelApp:
    def __init__(
        self,
        workbooks: (
            _OpenParityWorkbooks
            | _NoneReturningOpenWorkbooks
            | _StaleActiveOpenWorkbooks
            | _CountFailOpenWorkbooks
            | _CurrentCountFailOpenWorkbooks
            | _IndexFailOpenWorkbooks
        ),
    ) -> None:
        self.Workbooks = workbooks
        self.Calculation = -4105
        self.Hwnd = 1
        self.AutomationSecurity = None

    @property
    def ActiveWorkbook(self):
        if hasattr(self.Workbooks, "active_workbook"):
            return self.Workbooks.active_workbook
        try:
            return self.Workbooks(1)
        except Exception:
            return None

    def CalculateFullRebuild(self) -> None:
        return None

    def CalculateUntilAsyncQueriesDone(self) -> None:
        return None

    def Quit(self) -> None:
        return None


class _CaptureStore:
    db_path = Path("capture.sqlite3")

    def __init__(self) -> None:
        self.finish_calls: list[dict[str, object]] = []

    def finish(self, **kwargs) -> None:
        self.finish_calls.append(kwargs)

    def close(self) -> None:
        pass


async def main_async() -> int:
    for raw_path in (
        "C:\\Users\\Jane Doe\\AppData\\Local\\Temp\\file.xlsx",
        "C:\\secret dir\\job\\workbook.xlsx",
        "\\\\server\\share name\\job\\workbook.xlsx",
        "/tmp/my dir/job/workbook.xlsx",
        "C:\\Users\\Jane Doe\\Secret Dir",
        "\\\\server\\share name\\Secret Dir",
        "/tmp/my dir/secret dir",
    ):
        public_msg = public_worker_message(f"open failed for {raw_path}", fallback="")
        _assert(raw_path not in public_msg, f"path was not redacted: {public_msg!r}")
        _assert("<path>" in public_msg, f"redaction marker missing: {public_msg!r}")
        _assert("Jane Doe" not in public_msg, f"spaced Windows path leaked: {public_msg!r}")
        _assert("secret dir" not in public_msg, f"spaced Windows path leaked: {public_msg!r}")
        _assert("share name" not in public_msg, f"spaced UNC path leaked: {public_msg!r}")
        _assert("my dir" not in public_msg, f"spaced POSIX path leaked: {public_msg!r}")

    suffix_msg = public_worker_message(
        "Failed to read sample metadata from C:\\secret dir\\thread_1: [WinError 5] Access is denied",
        fallback="",
    )
    _assert("C:\\secret" not in suffix_msg, f"path-bearing suffix message leaked: {suffix_msg!r}")
    _assert("<path>" in suffix_msg, f"path-bearing suffix message lost redaction marker: {suffix_msg!r}")
    _assert("Access is denied" in suffix_msg, f"path redaction dropped useful suffix: {suffix_msg!r}")

    _assert(_coerce_ready_pid(1234) == 1234, "valid ready pid was rejected")
    for bad_pid in (True, False, 0, -1, "1234", None):
        _assert(_coerce_ready_pid(bad_pid) is None, f"invalid ready pid was accepted: {bad_pid!r}")

    unicode_msg = "R\u00e9sum\u00e9 \u8868"
    stdout_buffer = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = stdout_buffer
    try:
        excel_worker_server_mod._send({"type": "result", "job_id": "unicode", "ok": True, "msg": unicode_msg})
    finally:
        sys.stdout = original_stdout
    sent_text = stdout_buffer.getvalue()
    _assert(unicode_msg not in sent_text, f"worker stdout JSON was not ASCII-safe: {sent_text!r}")
    _assert(json.loads(sent_text)["msg"] == unicode_msg, "worker stdout ASCII JSON changed payload")

    ascii_protocol_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    ascii_protocol_proc = _FakeStartedProc()
    ascii_protocol_worker._proc = ascii_protocol_proc
    ascii_protocol_worker._rx.put_nowait(
        {"type": "result", "job_id": "unicode-job", "ok": True, "reward": 1.0, "msg": ""}
    )
    await ascii_protocol_worker.run_job(
        job_id="unicode-job",
        gt_file=Path(f"{unicode_msg}.xlsx"),
        proc_file=Path("processed.xlsx"),
        answer_position=f"{unicode_msg}!A1",
        timeout_s=1.0,
    )
    request_text = ascii_protocol_proc.stdin.writes[-1]
    _assert(
        unicode_msg not in request_text,
        f"pooled worker request JSON was not ASCII-safe: {request_text!r}",
    )
    request_payload = json.loads(request_text)
    _assert(request_payload["answer_position"] == f"{unicode_msg}!A1", "ASCII request JSON changed payload")

    blocking_write_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    blocking_write_proc = _BlockingWriteProc()
    blocking_write_worker._proc = blocking_write_proc
    blocking_write_task = asyncio.create_task(
        blocking_write_worker.run_job(
            job_id="blocking-write",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=0.2,
        )
    )
    try:
        write_started = await asyncio.to_thread(blocking_write_proc.stdin.write_started.wait, 1.0)
        _assert(write_started, "pooled worker stdin write did not start")
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=0.2)
        try:
            await blocking_write_task
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("blocked pooled stdin write did not time out")
    finally:
        blocking_write_proc.stdin.write_release.set()

    original_popen = excel_pool_mod.subprocess.Popen
    original_stdout_loop = ExcelWorkerProcess._stdout_loop
    original_stderr_loop = ExcelWorkerProcess._stderr_loop
    original_creation_time = windows_process_mod._process_creation_time
    original_powershell_creation_time = windows_process_mod._windows_powershell_creation_time
    original_taskkill_tree = windows_process_mod._taskkill_tree
    original_pool_taskkill_tree = excel_pool_mod._taskkill_tree
    started_procs: list[_FakeStartedProc] = []
    taskkill_calls: list[int] = []

    def fake_popen(*args, **kwargs):
        proc = _FakeStartedProc()
        started_procs.append(proc)
        return proc

    def record_taskkill_tree(pid: int) -> None:
        taskkill_calls.append(pid)

    excel_pool_mod.subprocess.Popen = fake_popen
    ExcelWorkerProcess._stdout_loop = lambda self: None
    ExcelWorkerProcess._stderr_loop = lambda self: None
    windows_process_mod._process_creation_time = lambda pid: None
    windows_process_mod._windows_powershell_creation_time = lambda pid: None
    windows_process_mod._taskkill_tree = record_taskkill_tree
    excel_pool_mod._taskkill_tree = record_taskkill_tree
    try:
        for payload, expected_detail in (
            ({"type": "ready", "worker_pid": 999999, "excel_pid": 4321}, "worker reported unexpected worker_pid"),
            ({"type": "ready", "worker_pid": 123456, "excel_pid": True}, "worker did not report excel_pid"),
            ({"type": "ready", "worker_pid": 123456, "excel_pid": 0}, "worker did not report excel_pid"),
            ({"type": "ready", "worker_pid": 123456, "excel_pid": 4321}, "worker did not report verifiable excel_pid"),
        ):
            pooled_start_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
            pooled_start_worker._rx.put_nowait(payload)
            try:
                await pooled_start_worker.start()
            except WorkerProtocolError as exc:
                _assert(expected_detail in str(exc), f"unexpected ready metadata error: {exc}")
            else:
                raise AssertionError(f"invalid ready metadata was accepted: {payload!r}")
            finally:
                await pooled_start_worker.shutdown(force=True)
            shutdown_writes = [
                text
                for text in started_procs[-1].stdin.writes
                if '"type": "shutdown"' in text
            ]
            _assert(shutdown_writes, f"invalid ready metadata did not trigger graceful shutdown: {payload!r}")

        creation_times = {999999: 10, 4321: 20}
        windows_process_mod._process_creation_time = lambda pid: creation_times.get(pid)
        wrapper_start_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
        wrapper_start_worker._rx.put_nowait({"type": "ready", "worker_pid": 999999, "excel_pid": 4321})
        await wrapper_start_worker.start()
        _assert(
            wrapper_start_worker.worker_pid == 999999,
            f"wrapper worker pid was not recorded: {wrapper_start_worker.worker_pid}",
        )
        _assert(
            wrapper_start_worker.worker_creation_time == 10,
            f"wrapper worker creation time was not recorded: {wrapper_start_worker.worker_creation_time}",
        )
        await wrapper_start_worker.shutdown(force=True)
        _assert(
            999999 in taskkill_calls,
            f"force shutdown did not target verified worker pid: {taskkill_calls}",
        )

        cancelled_start_worker = ExcelWorkerProcess(
            platform="windows",
            loop=asyncio.get_running_loop(),
            startup_timeout_s=60.0,
        )
        cancelled_start_task = asyncio.create_task(cancelled_start_worker.start())
        await asyncio.sleep(0)
        cancelled_start_task.cancel()
        try:
            await cancelled_start_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled pooled worker start did not raise CancelledError")
        _assert(
            taskkill_calls and taskkill_calls[-1] == 123456,
            f"cancelled pooled worker start did not force-kill process tree: {taskkill_calls}",
        )
        _assert(cancelled_start_worker._proc is None, "cancelled pooled worker start retained proc")

        timeout_start_worker = ExcelWorkerProcess(
            platform="windows",
            loop=asyncio.get_running_loop(),
            startup_timeout_s=0.01,
        )
        try:
            await timeout_start_worker.start()
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("timed-out pooled worker start did not raise TimeoutError")
        _assert(
            taskkill_calls and taskkill_calls[-1] == 123456,
            f"timed-out pooled worker start did not force-kill process tree: {taskkill_calls}",
        )
        _assert(timeout_start_worker._proc is None, "timed-out pooled worker start retained proc")

        blocking_shutdown_worker = ExcelWorkerProcess(
            platform="windows",
            loop=asyncio.get_running_loop(),
        )
        blocking_shutdown_proc = _BlockingWriteProc()
        blocking_shutdown_worker._proc = blocking_shutdown_proc
        blocking_shutdown_task = asyncio.create_task(blocking_shutdown_worker.shutdown(force=False))
        try:
            write_started = await asyncio.to_thread(blocking_shutdown_proc.stdin.write_started.wait, 1.0)
            _assert(write_started, "pooled worker shutdown stdin write did not start")
            await asyncio.wait_for(asyncio.sleep(0.05), timeout=0.2)
        finally:
            blocking_shutdown_proc.stdin.write_release.set()
        await blocking_shutdown_task
        _assert(
            any('"type": "shutdown"' in text for text in blocking_shutdown_proc.stdin.writes),
            "pooled worker shutdown did not send graceful shutdown request",
        )

        graceful_shutdown_worker = ExcelWorkerProcess(
            platform="windows",
            loop=asyncio.get_running_loop(),
        )
        graceful_shutdown_worker._proc = _FakeStartedProc()
        graceful_shutdown_worker.excel_pid = 4321
        graceful_shutdown_worker.excel_creation_time = 987654
        process_exists_calls: list[int] = []
        taskkill_pid_calls: list[tuple[int, int | None]] = []
        original_process_exists = excel_pool_mod._process_exists
        original_taskkill_pid = windows_process_mod._taskkill_pid
        original_pool_taskkill_pid = excel_pool_mod._taskkill_pid

        def record_process_exists(pid: int) -> bool:
            process_exists_calls.append(pid)
            return True

        def record_taskkill_pid(pid: int, expected_creation_time: int | None = None) -> None:
            taskkill_pid_calls.append((pid, expected_creation_time))

        excel_pool_mod._process_exists = record_process_exists
        windows_process_mod._taskkill_pid = record_taskkill_pid
        excel_pool_mod._taskkill_pid = record_taskkill_pid
        try:
            await graceful_shutdown_worker.shutdown(force=False)
        finally:
            excel_pool_mod._process_exists = original_process_exists
            windows_process_mod._taskkill_pid = original_taskkill_pid
            excel_pool_mod._taskkill_pid = original_pool_taskkill_pid
        _assert(process_exists_calls == [4321], f"graceful shutdown did not verify Excel PID: {process_exists_calls}")
        _assert(
            taskkill_pid_calls == [(4321, 987654)],
            f"graceful shutdown did not kill surviving Excel PID: {taskkill_pid_calls}",
        )

        cancelled_shutdown_worker = ExcelWorkerProcess(
            platform="windows",
            loop=asyncio.get_running_loop(),
        )
        cancelled_shutdown_proc = _BlockingWaitProc()
        cancelled_shutdown_worker._proc = cancelled_shutdown_proc
        cancelled_shutdown_worker.excel_pid = 8765
        cancelled_shutdown_worker.excel_creation_time = 5678
        cancelled_shutdown_pid_calls: list[tuple[int, int | None]] = []
        original_taskkill_pid = windows_process_mod._taskkill_pid
        original_pool_taskkill_pid = excel_pool_mod._taskkill_pid
        windows_process_mod._taskkill_pid = (
            lambda pid, expected_creation_time=None: cancelled_shutdown_pid_calls.append(
                (pid, expected_creation_time)
            )
        )
        excel_pool_mod._taskkill_pid = windows_process_mod._taskkill_pid
        try:
            cancelled_shutdown_task = asyncio.create_task(cancelled_shutdown_worker.shutdown(force=False))
            started = await asyncio.to_thread(cancelled_shutdown_proc.wait_started.wait, 5.0)
            _assert(started, "pooled worker shutdown did not start graceful wait")
            cancelled_shutdown_task.cancel()
            await asyncio.sleep(0)
            cancelled_shutdown_proc.wait_release.set()
            try:
                await cancelled_shutdown_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled pooled worker shutdown did not raise CancelledError")
        finally:
            cancelled_shutdown_proc.wait_release.set()
            windows_process_mod._taskkill_pid = original_taskkill_pid
            excel_pool_mod._taskkill_pid = original_pool_taskkill_pid
        _assert(
            taskkill_calls and taskkill_calls[-1] == 123456,
            f"cancelled pooled worker shutdown did not force-kill process tree: {taskkill_calls}",
        )
        _assert(
            cancelled_shutdown_pid_calls and cancelled_shutdown_pid_calls[-1] == (8765, 5678),
            f"cancelled pooled worker shutdown did not force-clean Excel PID: {cancelled_shutdown_pid_calls}",
        )
    finally:
        excel_pool_mod.subprocess.Popen = original_popen
        ExcelWorkerProcess._stdout_loop = original_stdout_loop
        ExcelWorkerProcess._stderr_loop = original_stderr_loop
        windows_process_mod._process_creation_time = original_creation_time
        windows_process_mod._windows_powershell_creation_time = original_powershell_creation_time
        windows_process_mod._taskkill_tree = original_taskkill_tree
        excel_pool_mod._taskkill_tree = original_pool_taskkill_tree

    with temporary_directory(prefix="async_reward_api_worker_cancel_") as tmp:
        pid_file = Path(tmp) / "worker.pid"
        script = (
            "import os, pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        worker_task = asyncio.create_task(
            manager_mod._run_worker_subprocess(
                cmd=[sys.executable, "-c", script, str(pid_file)],
                timeout_s=60.0,
            )
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.05)
        _assert(pid_file.exists(), "test worker did not start")
        worker_pid = int(pid_file.read_text(encoding="utf-8"))
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled per-job worker task did not raise CancelledError")

        for _ in range(100):
            if not _pid_is_running(worker_pid):
                break
            await asyncio.sleep(0.05)
        _assert(not _pid_is_running(worker_pid), "cancelled per-job worker process was still running")

    class _BlockingCommunicateProc:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def communicate(self, timeout=None):
            self.started.set()
            self.release.wait(timeout=5.0)
            return b"", b""

    blocking_communicate_proc = _BlockingCommunicateProc()
    blocking_communicate_kills: list[object] = []
    original_start_worker_subprocess = manager_mod._start_worker_subprocess
    original_kill_subprocess_tree = manager_mod._kill_subprocess_tree
    manager_mod._start_worker_subprocess = lambda *, cmd: blocking_communicate_proc
    manager_mod._kill_subprocess_tree = lambda proc: blocking_communicate_kills.append(proc)
    repeated_cancel_task = asyncio.create_task(
        manager_mod._run_worker_subprocess(cmd=["worker"], timeout_s=60.0)
    )
    try:
        started = await asyncio.to_thread(blocking_communicate_proc.started.wait, 1.0)
        _assert(started, "blocking communicate worker did not start")
        repeated_cancel_task.cancel()
        await asyncio.sleep(0)
        repeated_cancel_task.cancel()
        await asyncio.sleep(0.05)
        _assert(
            not repeated_cancel_task.done(),
            "repeated cancellation interrupted worker subprocess cleanup",
        )
        blocking_communicate_proc.release.set()
        try:
            await asyncio.wait_for(repeated_cancel_task, timeout=1.0)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("repeated-cancel worker task did not raise CancelledError")
    finally:
        blocking_communicate_proc.release.set()
        manager_mod._start_worker_subprocess = original_start_worker_subprocess
        manager_mod._kill_subprocess_tree = original_kill_subprocess_tree
    _assert(
        blocking_communicate_kills == [blocking_communicate_proc],
        "repeated-cancel worker cleanup did not kill subprocess",
    )

    class _CommunicateFailProc:
        def communicate(self, timeout=None):
            raise RuntimeError("communicate failed")

    communicate_fail_proc = _CommunicateFailProc()
    communicate_kills: list[object] = []
    original_start_worker_subprocess = manager_mod._start_worker_subprocess
    original_kill_subprocess_tree = manager_mod._kill_subprocess_tree
    manager_mod._start_worker_subprocess = lambda *, cmd: communicate_fail_proc
    manager_mod._kill_subprocess_tree = lambda proc: communicate_kills.append(proc)
    try:
        try:
            await manager_mod._run_worker_subprocess(cmd=["worker"], timeout_s=1.0)
        except RuntimeError as exc:
            _assert("communicate failed" in str(exc), f"unexpected communicate failure: {exc}")
        else:
            raise AssertionError("unexpected communicate failure did not propagate")
    finally:
        manager_mod._start_worker_subprocess = original_start_worker_subprocess
        manager_mod._kill_subprocess_tree = original_kill_subprocess_tree
    _assert(
        communicate_kills == [communicate_fail_proc],
        "unexpected communicate failure did not kill worker subprocess",
    )

    class _NonzeroCommunicateProc:
        def __init__(self) -> None:
            self.returncode = None

        def communicate(self, timeout=None):
            self.returncode = 2
            noise = "\n".join(f"worker noise line {i}" for i in range(80))
            return (
                b"",
                (
                    f"{noise}\n[worker] fatal recalc failed: "
                    "Excel recalc fatal failure: close failed for C:\\secret\\job\\workbook.xlsx"
                ).encode(),
            )

    nonzero_proc = _NonzeroCommunicateProc()
    nonzero_kills: list[object] = []
    original_start_worker_subprocess = manager_mod._start_worker_subprocess
    original_kill_subprocess_tree = manager_mod._kill_subprocess_tree
    manager_mod._start_worker_subprocess = lambda *, cmd: nonzero_proc
    manager_mod._kill_subprocess_tree = lambda proc: nonzero_kills.append(proc)
    try:
        try:
            await manager_mod._run_worker_subprocess(cmd=["worker"], timeout_s=1.0)
        except manager_mod.ControlledWorkerExitedError as exc:
            _assert("returncode=2" in str(exc), f"unexpected nonzero worker failure: {exc}")
            _assert("fatal recalc failed" in str(exc), f"nonzero worker stderr detail was lost: {exc}")
            _assert("close failed" in str(exc), f"nonzero worker terminal stderr detail was lost: {exc}")
            _assert("C:\\secret" not in str(exc), f"nonzero worker stderr leaked path: {exc}")
        except manager_mod.WorkerExitedError as exc:
            raise AssertionError(f"controlled fatal worker exit used generic WorkerExitedError: {exc}") from exc
        else:
            raise AssertionError("nonzero worker exit did not propagate")
    finally:
        manager_mod._start_worker_subprocess = original_start_worker_subprocess
        manager_mod._kill_subprocess_tree = original_kill_subprocess_tree
    _assert(nonzero_kills == [], "nonzero completed worker exit should not kill subprocess tree")

    class _GenericNonzeroCommunicateProc:
        def __init__(self) -> None:
            self.returncode = None

        def communicate(self, timeout=None):
            self.returncode = 1
            return b"", b"worker import failed for C:\\secret\\job\\workbook.xlsx"

    generic_nonzero_proc = _GenericNonzeroCommunicateProc()
    generic_nonzero_kills: list[object] = []
    original_start_worker_subprocess = manager_mod._start_worker_subprocess
    original_kill_subprocess_tree = manager_mod._kill_subprocess_tree
    manager_mod._start_worker_subprocess = lambda *, cmd: generic_nonzero_proc
    manager_mod._kill_subprocess_tree = lambda proc: generic_nonzero_kills.append(proc)
    try:
        try:
            await manager_mod._run_worker_subprocess(cmd=["worker"], timeout_s=1.0)
        except manager_mod.ControlledWorkerExitedError as exc:
            raise AssertionError(f"generic nonzero worker exit was classified as controlled: {exc}") from exc
        except manager_mod.WorkerExitedError as exc:
            _assert("returncode=1" in str(exc), f"generic nonzero return code was lost: {exc}")
            _assert("worker import failed" in str(exc), f"generic nonzero stderr detail was lost: {exc}")
            _assert("C:\\secret" not in str(exc), f"generic nonzero worker stderr leaked path: {exc}")
        else:
            raise AssertionError("generic nonzero worker exit did not propagate")
    finally:
        manager_mod._start_worker_subprocess = original_start_worker_subprocess
        manager_mod._kill_subprocess_tree = original_kill_subprocess_tree
    _assert(generic_nonzero_kills == [], "generic nonzero completed worker exit should not kill subprocess tree")

    session = _WindowsExcelSession()
    session._xl_app = _FakeExcelApp(
        _CloseFailWorkbook(),
        expected_filename=os.path.abspath("workbook.xlsx"),
    )
    try:
        session.recalc_and_save(Path("workbook.xlsx"))
    except FatalExcelSessionError as exc:
        _assert("close workbook" in str(exc), f"unexpected close failure: {exc}")
        _assert("close failed" in str(exc), f"target close failure detail was lost: {exc}")
    else:
        raise AssertionError("pooled Excel session ignored workbook close failure")

    save_cleanup_workbooks = _OpenParityWorkbooks(expected_filename=os.path.abspath("workbook.xlsx"))
    save_cleanup_workbooks.workbook = _SaveAndCloseFailOpenParityWorkbook(
        full_name=os.path.abspath("workbook.xlsx"),
        save_error="save failed " + ("x" * 600),
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(save_cleanup_workbooks)
    try:
        session.recalc_and_save(Path("workbook.xlsx"))
    except FatalExcelSessionError as exc:
        _assert("close workbook" in str(exc), f"cleanup close wrapper detail was lost: {exc}")
        _assert("save failed" in str(exc), f"cleanup close root operation detail was lost: {exc}")
        _assert("close failed" in str(exc), f"cleanup close terminal detail was lost: {exc}")
    else:
        raise AssertionError("pooled Excel session ignored cleanup close failure after save failure")
    _assert(save_cleanup_workbooks.workbook.save_calls == 1, "pooled save failure setup did not run")
    _assert(
        save_cleanup_workbooks.workbook.close_save_changes == [False],
        "pooled cleanup close was not attempted after save failure",
    )

    close_mode_workbook = _OpenParityWorkbook()
    session = _WindowsExcelSession()
    session._xl_app = _FakeExcelApp(
        close_mode_workbook,
        expected_filename=os.path.abspath("workbook.xlsx"),
    )
    session.recalc_and_save(Path("workbook.xlsx"))
    _assert(
        close_mode_workbook.close_save_changes == [False],
        f"pooled Excel session close save mode changed: {close_mode_workbook.close_save_changes}",
    )

    target_path = Path("workbook.xlsx")
    open_parity_workbooks = _OpenParityWorkbooks(expected_filename=os.path.abspath(str(target_path)))
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(open_parity_workbooks)
    session.recalc_and_save(target_path)
    _assert(
        open_parity_workbooks.calls and open_parity_workbooks.calls[0].get("CorruptLoad") == 1,
        f"pooled Excel session first open did not use CorruptLoad: {open_parity_workbooks.calls}",
    )
    _assert(
        open_parity_workbooks.workbook.close_save_changes == [False],
        "pooled open-parity workbook was not closed",
    )

    none_open_workbooks = _NoneReturningOpenWorkbooks(full_name=os.path.abspath(str(target_path)))
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(none_open_workbooks)
    session.recalc_and_save(target_path)
    _assert(
        len(none_open_workbooks.calls) == 1,
        f"pooled Excel session retried after recovering None open result: {none_open_workbooks.calls}",
    )
    _assert(none_open_workbooks.workbook.save_calls == 1, "pooled recovered workbook was not saved")
    _assert(
        none_open_workbooks.workbook.close_save_changes == [False],
        "pooled Excel session did not close workbook recovered after None open result",
    )
    _assert(
        none_open_workbooks.preexisting_workbook.close_save_changes == [],
        "pooled pre-existing workbook was closed during recovery",
    )

    fresh_none_open_workbooks = _NoneReturningOpenWorkbooks(
        full_name=os.path.abspath(str(target_path)),
        preexisting_full_name=None,
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(fresh_none_open_workbooks)
    session.recalc_and_save(target_path)
    _assert(fresh_none_open_workbooks.workbook.save_calls == 1, "pooled index-1 recovered workbook was not saved")
    _assert(
        fresh_none_open_workbooks.workbook.close_save_changes == [False],
        "pooled index-1 recovered workbook was not closed",
    )

    multi_open_workbooks = _NoneReturningOpenWorkbooks(
        full_name=os.path.abspath(str(target_path)),
        extra_full_names=(os.path.abspath("unexpected.xlsx"),),
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(multi_open_workbooks)
    session.recalc_and_save(target_path)
    _assert(multi_open_workbooks.workbook.save_calls == 1, "pooled multi-open target was not saved")
    _assert(
        multi_open_workbooks.workbook.close_save_changes == [False],
        "pooled multi-open target was not closed",
    )
    _assert(
        multi_open_workbooks.extra_workbooks[0].close_save_changes == [False],
        "pooled multi-open mismatched workbook was not closed",
    )

    duplicate_match_workbooks = _NoneReturningOpenWorkbooks(
        full_name=os.path.abspath(str(target_path)),
        extra_full_names=(os.path.abspath(str(target_path)),),
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(duplicate_match_workbooks)
    session.recalc_and_save(target_path)
    _assert(duplicate_match_workbooks.workbook.save_calls == 1, "pooled duplicate-match target was not saved")
    _assert(
        duplicate_match_workbooks.workbook.close_save_changes == [False],
        "pooled duplicate-match target was not closed",
    )
    _assert(
        duplicate_match_workbooks.extra_workbooks[0].close_save_changes == [False],
        "pooled duplicate matching workbook was not closed",
    )

    mismatch_open_workbooks = _NoneReturningOpenWorkbooks(
        full_name=os.path.abspath(str(target_path)),
        workbook_full_name=os.path.abspath(str(Path("other") / "workbook.xlsx")),
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(mismatch_open_workbooks)
    try:
        session.recalc_and_save(Path("workbook.xlsx"))
    except FatalExcelSessionError as exc:
        raise AssertionError("pooled mismatched recovered workbook was incorrectly fatal") from exc
    except RuntimeError as exc:
        _assert("failed to open workbook" in str(exc), f"unexpected mismatched workbook failure: {exc}")
    else:
        raise AssertionError("pooled Excel session accepted mismatched recovered workbook")
    _assert(mismatch_open_workbooks.workbook.save_calls == 0, "pooled mismatched recovered workbook was saved")
    _assert(
        mismatch_open_workbooks.workbook.close_save_changes == [False, False, False],
        "pooled mismatched recovered workbook was not closed",
    )

    fatal_mismatch_workbooks = _NoneReturningOpenWorkbooks(
        full_name=os.path.abspath(str(target_path)),
        workbook_full_name=os.path.abspath(str(Path("other") / "workbook.xlsx")),
    )
    fatal_mismatch_workbooks.workbook = _CloseFailOpenParityWorkbook(
        full_name=os.path.abspath(str(Path("other") / "workbook.xlsx"))
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(fatal_mismatch_workbooks)
    try:
        session.recalc_and_save(Path("workbook.xlsx"))
    except FatalExcelSessionError as exc:
        _assert("close failed" in str(exc), f"fatal close detail was lost: {exc}")
    else:
        raise AssertionError("pooled Excel session did not treat rejected workbook close failure as fatal")
    _assert(
        fatal_mismatch_workbooks.workbook.close_save_changes == [False],
        "pooled fatal mismatched workbook close was not attempted",
    )

    count_fail_workbooks = _CountFailOpenWorkbooks(full_name=os.path.abspath(str(target_path)))
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(count_fail_workbooks)
    try:
        session.recalc_and_save(target_path)
    except FatalExcelSessionError as exc:
        _assert("prior workbook count" in str(exc), f"wrong prior-count fatal detail: {exc}")
        _assert("count failed" in str(exc), f"prior-count terminal detail was lost: {exc}")
    else:
        raise AssertionError("pooled Excel session did not treat unavailable workbook count as fatal")

    current_count_fail_workbooks = _CurrentCountFailOpenWorkbooks(full_name=os.path.abspath(str(target_path)))
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(current_count_fail_workbooks)
    try:
        session.recalc_and_save(target_path)
    except FatalExcelSessionError as exc:
        _assert("current workbook count" in str(exc), f"wrong current-count fatal detail: {exc}")
        _assert("current count failed" in str(exc), f"current-count terminal detail was lost: {exc}")
    else:
        raise AssertionError("pooled Excel session did not treat unavailable current workbook count as fatal")

    index_fail_workbooks = _IndexFailOpenWorkbooks(full_name=os.path.abspath(str(target_path)))
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(index_fail_workbooks)
    try:
        session.recalc_and_save(target_path)
    except FatalExcelSessionError as exc:
        _assert("new workbook was inaccessible" in str(exc), f"wrong index fatal detail: {exc}")
        _assert("index failed" in str(exc), f"index terminal detail was lost: {exc}")
    else:
        raise AssertionError("pooled Excel session did not treat inaccessible new workbook as fatal")

    stale_open_workbooks = _StaleActiveOpenWorkbooks(
        stale_full_name=os.path.abspath("stale.xlsx"),
        expected_filename=os.path.abspath(str(target_path)),
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(stale_open_workbooks)
    try:
        session.recalc_and_save(Path("workbook.xlsx"))
    except FatalExcelSessionError as exc:
        raise AssertionError("pooled stale ActiveWorkbook was incorrectly fatal") from exc
    except RuntimeError as exc:
        _assert("failed to open workbook" in str(exc), f"unexpected stale active workbook failure: {exc}")
    else:
        raise AssertionError("pooled Excel session accepted stale ActiveWorkbook after None open result")
    _assert(stale_open_workbooks.workbook.save_calls == 0, "pooled stale ActiveWorkbook was saved")
    _assert(
        stale_open_workbooks.workbook.close_save_changes == [],
        "pooled stale ActiveWorkbook was closed as if it were the target",
    )

    same_path_stale_workbooks = _StaleActiveOpenWorkbooks(
        stale_full_name=os.path.abspath("workbook.xlsx"),
        expected_filename=os.path.abspath(str(target_path)),
    )
    session = _WindowsExcelSession()
    session._xl_app = _OpenParityExcelApp(same_path_stale_workbooks)
    try:
        session.recalc_and_save(Path("workbook.xlsx"))
    except FatalExcelSessionError as exc:
        raise AssertionError("pooled same-path stale ActiveWorkbook was incorrectly fatal") from exc
    except RuntimeError as exc:
        _assert("failed to open workbook" in str(exc), f"unexpected same-path stale workbook failure: {exc}")
    else:
        raise AssertionError("pooled Excel session accepted same-path stale ActiveWorkbook after None open result")
    _assert(same_path_stale_workbooks.workbook.save_calls == 0, "pooled same-path stale workbook was saved")
    _assert(
        same_path_stale_workbooks.workbook.close_save_changes == [],
        "pooled same-path stale workbook was closed as if it were newly opened",
    )

    fatal_main_sessions = []

    class _FatalMainSession:
        excel_pid = 777

        def __init__(self) -> None:
            self.shutdown_called = False
            fatal_main_sessions.append(self)

        def start(self) -> None:
            return None

        def recalc_and_save(self, proc_file: Path) -> None:
            raise FatalExcelSessionError("fatal close failed")

        def shutdown(self) -> None:
            self.shutdown_called = True

    original_excel_session_cls = excel_worker_server_mod._WindowsExcelSession
    original_stdin = sys.stdin
    excel_worker_server_mod._WindowsExcelSession = _FatalMainSession
    sys.stdin = io.StringIO(
        json.dumps({"type": "recalc", "job_id": "fatal-job", "proc_file": "workbook.xlsx"}) + "\n"
    )
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = excel_worker_server_mod.main(["--platform", "windows"])
    finally:
        sys.stdin = original_stdin
        excel_worker_server_mod._WindowsExcelSession = original_excel_session_cls
    _assert(rc == 1, f"fatal pooled worker main should exit nonzero: {rc}")
    main_messages = [
        json.loads(line)
        for line in stdout.getvalue().splitlines()
        if line.strip()
    ]
    _assert(
        len(main_messages) == 2
        and main_messages[0].get("type") == "ready"
        and main_messages[1].get("ok") is False
        and "fatal close failed" in str(main_messages[1].get("msg")),
        f"fatal pooled worker main did not emit fatal result detail: {main_messages}",
    )
    _assert("fatal worker error" in stderr.getvalue(), "fatal pooled worker main did not log fatal error")
    _assert("fatal close failed" in stderr.getvalue(), "fatal pooled worker main lost fatal detail")
    _assert(fatal_main_sessions and fatal_main_sessions[0].shutdown_called, "fatal pooled worker did not shutdown")

    startup_calls = {"coinitialize": 0, "couninitialize": 0, "quit": 0}
    pythoncom_mod = types.ModuleType("pythoncom")

    def fake_coinitialize() -> None:
        startup_calls["coinitialize"] += 1

    def fake_couninitialize() -> None:
        startup_calls["couninitialize"] += 1

    pythoncom_mod.CoInitialize = fake_coinitialize
    pythoncom_mod.CoUninitialize = fake_couninitialize
    win32process_mod = types.ModuleType("win32process")
    win32process_mod.GetWindowThreadProcessId = lambda hwnd: (0, 1234)
    win32com_mod = types.ModuleType("win32com")
    win32com_mod.__path__ = []
    win32com_client_mod = types.ModuleType("win32com.client")

    class _StartupFailExcelApp:
        @property
        def Visible(self):
            return False

        @Visible.setter
        def Visible(self, value) -> None:
            raise RuntimeError("startup property failed")

        def Quit(self) -> None:
            startup_calls["quit"] += 1

    win32com_client_mod.DispatchEx = lambda prog_id: _StartupFailExcelApp()
    saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32process", "win32com", "win32com.client")}
    sys.modules["pythoncom"] = pythoncom_mod
    sys.modules["win32process"] = win32process_mod
    sys.modules["win32com"] = win32com_mod
    sys.modules["win32com.client"] = win32com_client_mod
    try:
        startup_session = _WindowsExcelSession()
        try:
            startup_session.start()
        except RuntimeError as exc:
            _assert("startup property failed" in str(exc), f"unexpected startup failure: {exc}")
        else:
            raise AssertionError("Excel startup failure did not propagate")
        _assert(startup_calls["coinitialize"] == 1, "COM was not initialized during startup")
        _assert(startup_calls["quit"] == 1, "partial Excel startup did not quit the application")
        _assert(startup_calls["couninitialize"] == 1, "partial Excel startup did not uninitialize COM")
        _assert(startup_session._pythoncom is None, "partial Excel startup left COM reference attached")
    finally:
        for name, original_module in saved_modules.items():
            if original_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original_module

    startup_success_calls = {"coinitialize": 0, "couninitialize": 0}
    startup_success_app = _OpenParityExcelApp(_OpenParityWorkbooks())
    pythoncom_mod = types.ModuleType("pythoncom")
    pythoncom_mod.CoInitialize = lambda: startup_success_calls.__setitem__(
        "coinitialize",
        startup_success_calls["coinitialize"] + 1,
    )
    pythoncom_mod.CoUninitialize = lambda: startup_success_calls.__setitem__(
        "couninitialize",
        startup_success_calls["couninitialize"] + 1,
    )
    win32process_mod = types.ModuleType("win32process")
    win32process_mod.GetWindowThreadProcessId = lambda hwnd: (0, 2468)
    win32com_mod = types.ModuleType("win32com")
    win32com_mod.__path__ = []
    win32com_client_mod = types.ModuleType("win32com.client")
    win32com_client_mod.DispatchEx = lambda prog_id: startup_success_app
    saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32process", "win32com", "win32com.client")}
    sys.modules["pythoncom"] = pythoncom_mod
    sys.modules["win32process"] = win32process_mod
    sys.modules["win32com"] = win32com_mod
    sys.modules["win32com.client"] = win32com_client_mod
    try:
        startup_success_session = _WindowsExcelSession()
        startup_success_session.start()
        _assert(startup_success_app.AutomationSecurity == 3, "persistent Excel startup did not disable macros")
        _assert(startup_success_session.excel_pid == 2468, "persistent Excel startup PID changed")
        startup_success_session.shutdown()
        _assert(startup_success_calls["couninitialize"] == 1, "successful startup did not uninitialize COM")
    finally:
        for name, original_module in saved_modules.items():
            if original_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original_module

    with temporary_directory(prefix="async_reward_api_recalc_open_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        excel_pid_file = Path(tmp) / "excel_pid.json"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _OpenParityWorkbooks(expected_filename=os.path.abspath(str(workbook_path)))
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32process_mod = types.ModuleType("win32process")
        win32process_mod.GetWindowThreadProcessId = lambda hwnd: (0, 4321)
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32process", "win32com", "win32com.client")}
        original_creation_time = windows_process_mod._process_creation_time
        original_powershell_creation_time = windows_process_mod._windows_powershell_creation_time
        original_recalc_creation_time = recalc_mod._process_creation_time
        original_recalc_powershell_creation_time = recalc_mod._windows_powershell_creation_time
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32process"] = win32process_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        windows_process_mod._process_creation_time = lambda pid: None
        windows_process_mod._windows_powershell_creation_time = lambda pid: 987654
        recalc_mod._process_creation_time = lambda pid: None
        recalc_mod._windows_powershell_creation_time = lambda pid: 987654
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path, excel_pid_file=excel_pid_file)
        finally:
            windows_process_mod._process_creation_time = original_creation_time
            windows_process_mod._windows_powershell_creation_time = original_powershell_creation_time
            recalc_mod._process_creation_time = original_recalc_creation_time
            recalc_mod._windows_powershell_creation_time = original_recalc_powershell_creation_time
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 0, f"per-job recalc open with CorruptLoad failed: {msg}")
        _assert(open_app.AutomationSecurity == 3, "per-job Excel recalc did not disable macros")
        _assert(open_workbooks.calls, "per-job recalc did not open workbook")
        _assert(
            open_workbooks.calls[0].get("CorruptLoad") == 1,
            f"per-job recalc first open did not match persistent worker: {open_workbooks.calls}",
        )
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            f"per-job recalc close save mode changed: {open_workbooks.workbook.close_save_changes}",
        )
        pid_payload = json.loads(excel_pid_file.read_text(encoding="utf-8"))
        _assert(pid_payload == {"pid": 4321, "creation_time": 987654}, f"pid fallback failed: {pid_payload}")
        _assert(
            not list(Path(tmp).glob(f".{excel_pid_file.name}.*.tmp")),
            "atomic pid-file write left a temp file behind",
        )

        retry_pid_file = Path(tmp) / "retry_excel_pid.json"
        retry_pid_file.write_text("{", encoding="utf-8")
        original_kill_excel_pid = windows_process_mod._kill_excel_pid
        kill_pid_calls: list[dict[str, object]] = []

        def record_kill_excel_pid(*, platform: Platform, pid: int, expected_creation_time: int | None) -> bool:
            kill_pid_calls.append(
                {
                    "platform": platform,
                    "pid": pid,
                    "expected_creation_time": expected_creation_time,
                }
            )
            return True

        def complete_retry_pid_file() -> None:
            retry_pid_file.write_text(
                json.dumps({"pid": 4321, "creation_time": 987654}),
                encoding="utf-8",
            )

        windows_process_mod._kill_excel_pid = record_kill_excel_pid
        timer = threading.Timer(0.08, complete_retry_pid_file)
        try:
            timer.start()
            killed_retry_pid = windows_process_mod._kill_excel_pid_from_file(
                platform=Platform.WINDOWS,
                pid_file=retry_pid_file,
            )
        finally:
            timer.cancel()
            windows_process_mod._kill_excel_pid = original_kill_excel_pid
        _assert(killed_retry_pid, "partial pid-file retry did not eventually kill")
        _assert(
            kill_pid_calls == [
                {
                    "platform": Platform.WINDOWS,
                    "pid": 4321,
                    "expected_creation_time": 987654,
                }
            ],
            f"partial pid-file retry used wrong kill metadata: {kill_pid_calls}",
        )

    def run_recalc_with_fake_excel(workbook_path: Path, open_workbooks):
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            return recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module

    with temporary_directory(prefix="async_reward_api_recalc_open_none_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _NoneReturningOpenWorkbooks(full_name=os.path.abspath(str(workbook_path)))
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 0, f"per-job recalc did not recover None open result: {msg}")
        _assert(
            len(open_workbooks.calls) == 1,
            f"per-job recalc retried after recovering None open result: {open_workbooks.calls}",
        )
        _assert(open_workbooks.workbook.save_calls == 1, "per-job recovered workbook was not saved")
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            "per-job recalc did not close workbook recovered after None open result",
        )
        _assert(
            open_workbooks.preexisting_workbook.close_save_changes == [],
            "per-job pre-existing workbook was closed during recovery",
        )

    with temporary_directory(prefix="async_reward_api_recalc_open_none_fresh_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _NoneReturningOpenWorkbooks(
            full_name=os.path.abspath(str(workbook_path)),
            preexisting_full_name=None,
        )
        rc, msg = run_recalc_with_fake_excel(workbook_path, open_workbooks)
        _assert(rc == 0, f"per-job index-1 recalc did not recover None open result: {msg}")
        _assert(open_workbooks.workbook.save_calls == 1, "per-job index-1 recovered workbook was not saved")
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            "per-job index-1 recovered workbook was not closed",
        )

    with temporary_directory(prefix="async_reward_api_recalc_target_close_fail_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _OpenParityWorkbooks(expected_filename=os.path.abspath(str(workbook_path)))
        open_workbooks.workbook = _CloseFailOpenParityWorkbook(
            full_name=os.path.abspath(str(workbook_path))
        )
        rc, msg = run_recalc_with_fake_excel(workbook_path, open_workbooks)
        _assert(rc == 2, f"per-job target close failure should return fatal status: {rc}, {msg!r}")
        _assert("close workbook" in msg, f"per-job target close failure lost wrapper detail: {msg!r}")
        _assert("close failed" in msg, f"per-job target close failure lost terminal detail: {msg!r}")
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            "per-job target close failure did not attempt cleanup close",
        )

    with temporary_directory(prefix="async_reward_api_recalc_save_cleanup_close_fail_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _OpenParityWorkbooks(expected_filename=os.path.abspath(str(workbook_path)))
        open_workbooks.workbook = _SaveAndCloseFailOpenParityWorkbook(
            full_name=os.path.abspath(str(workbook_path)),
            save_error="save failed " + ("x" * 600),
        )
        rc, msg = run_recalc_with_fake_excel(workbook_path, open_workbooks)
        _assert(rc == 2, f"per-job cleanup close failure should return fatal status: {rc}, {msg!r}")
        _assert("close workbook" in msg, f"per-job cleanup close failure lost wrapper detail: {msg!r}")
        _assert("save failed" in msg, f"per-job cleanup close failure lost root operation detail: {msg!r}")
        _assert("close failed" in msg, f"per-job cleanup close failure lost terminal detail: {msg!r}")
        _assert(open_workbooks.workbook.save_calls == 1, "per-job save failure setup did not run")
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            "per-job cleanup close was not attempted after save failure",
        )

    with temporary_directory(prefix="async_reward_api_recalc_open_multi_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _NoneReturningOpenWorkbooks(
            full_name=os.path.abspath(str(workbook_path)),
            extra_full_names=(os.path.abspath(str(Path(tmp) / "unexpected.xlsx")),),
        )
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 0, f"per-job multi-open recalc did not recover target workbook: {msg}")
        _assert(open_workbooks.workbook.save_calls == 1, "per-job multi-open target was not saved")
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            "per-job multi-open target was not closed",
        )
        _assert(
            open_workbooks.extra_workbooks[0].close_save_changes == [False],
            "per-job multi-open mismatched workbook was not closed",
        )

    with temporary_directory(prefix="async_reward_api_recalc_open_duplicate_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _NoneReturningOpenWorkbooks(
            full_name=os.path.abspath(str(workbook_path)),
            extra_full_names=(os.path.abspath(str(workbook_path)),),
        )
        rc, msg = run_recalc_with_fake_excel(workbook_path, open_workbooks)
        _assert(rc == 0, f"per-job duplicate-match recalc did not recover target workbook: {msg}")
        _assert(open_workbooks.workbook.save_calls == 1, "per-job duplicate-match target was not saved")
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            "per-job duplicate-match target was not closed",
        )
        _assert(
            open_workbooks.extra_workbooks[0].close_save_changes == [False],
            "per-job duplicate matching workbook was not closed",
        )

    with temporary_directory(prefix="async_reward_api_recalc_open_mismatch_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _NoneReturningOpenWorkbooks(
            full_name=os.path.abspath(str(workbook_path)),
            workbook_full_name=os.path.abspath(str(Path(tmp) / "other" / "workbook.xlsx")),
        )
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 1, "per-job recalc accepted mismatched recovered workbook")
        _assert("Excel recalc failed" in msg, f"per-job mismatched workbook failure lost context: {msg!r}")
        _assert(open_workbooks.workbook.save_calls == 0, "per-job mismatched recovered workbook was saved")
        _assert(
            open_workbooks.workbook.close_save_changes == [False, False, False],
            "per-job mismatched recovered workbook was not closed",
        )

    with temporary_directory(prefix="async_reward_api_recalc_open_fatal_mismatch_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _NoneReturningOpenWorkbooks(
            full_name=os.path.abspath(str(workbook_path)),
            workbook_full_name=os.path.abspath(str(Path(tmp) / "other" / "workbook.xlsx")),
        )
        open_workbooks.workbook = _CloseFailOpenParityWorkbook(
            full_name=os.path.abspath(str(Path(tmp) / "other" / "workbook.xlsx"))
        )
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 2, f"per-job fatal mismatched workbook should return fatal status: {rc}, {msg!r}")
        _assert("fatal failure" in msg, f"per-job fatal mismatch lost fatal context: {msg!r}")
        _assert("close failed" in msg, f"per-job fatal mismatch lost terminal close detail: {msg!r}")
        _assert(
            open_workbooks.workbook.close_save_changes == [False],
            "per-job fatal mismatched workbook close was not attempted",
        )

    with temporary_directory(prefix="async_reward_api_recalc_open_fatal_count_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        fatal_count_cases = (
            (
                "prior count unavailable",
                _CountFailOpenWorkbooks(full_name=os.path.abspath(str(workbook_path))),
                "prior workbook count",
                "count failed",
            ),
            (
                "current count unavailable",
                _CurrentCountFailOpenWorkbooks(full_name=os.path.abspath(str(workbook_path))),
                "current workbook count",
                "current count failed",
            ),
            (
                "new workbook inaccessible",
                _IndexFailOpenWorkbooks(full_name=os.path.abspath(str(workbook_path))),
                "new workbook was inaccessible",
                "index failed",
            ),
        )
        for label, open_workbooks, expected_detail, terminal_detail in fatal_count_cases:
            rc, msg = run_recalc_with_fake_excel(workbook_path, open_workbooks)
            _assert(rc == 2, f"per-job {label} should return fatal status: {rc}, {msg!r}")
            _assert("fatal failure" in msg, f"per-job {label} lost fatal context: {msg!r}")
            _assert(expected_detail in msg, f"per-job {label} lost specific fatal detail: {msg!r}")
            _assert(terminal_detail in msg, f"per-job {label} lost terminal fatal detail: {msg!r}")

    with temporary_directory(prefix="async_reward_api_recalc_open_stale_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _StaleActiveOpenWorkbooks(
            stale_full_name=os.path.abspath(str(Path(tmp) / "stale.xlsx")),
            expected_filename=os.path.abspath(str(workbook_path)),
        )
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 1, "per-job recalc accepted stale ActiveWorkbook after None open result")
        _assert("Excel recalc failed" in msg, f"per-job stale ActiveWorkbook failure lost context: {msg!r}")
        _assert(open_workbooks.workbook.save_calls == 0, "per-job stale ActiveWorkbook was saved")
        _assert(
            open_workbooks.workbook.close_save_changes == [],
            "per-job stale ActiveWorkbook was closed as if it were the target",
        )

    with temporary_directory(prefix="async_reward_api_recalc_open_same_path_stale_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        open_workbooks = _StaleActiveOpenWorkbooks(
            stale_full_name=os.path.abspath(str(workbook_path)),
            expected_filename=os.path.abspath(str(workbook_path)),
        )
        open_app = _OpenParityExcelApp(open_workbooks)
        pythoncom_mod = types.ModuleType("pythoncom")
        pythoncom_mod.CoInitialize = lambda: None
        pythoncom_mod.CoUninitialize = lambda: None
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: open_app
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 1, "per-job recalc accepted same-path stale ActiveWorkbook after None open result")
        _assert("Excel recalc failed" in msg, f"per-job same-path stale failure lost context: {msg!r}")
        _assert(open_workbooks.workbook.save_calls == 0, "per-job same-path stale ActiveWorkbook was saved")
        _assert(
            open_workbooks.workbook.close_save_changes == [],
            "per-job same-path stale ActiveWorkbook was closed as if it were newly opened",
        )

    with temporary_directory(prefix="async_reward_api_recalc_coinitialize_") as tmp:
        workbook_path = Path(tmp) / "workbook.xlsx"
        workbook_path.write_bytes(b"placeholder")
        com_calls = {"couninitialize": 0}
        pythoncom_mod = types.ModuleType("pythoncom")

        def failing_coinitialize() -> None:
            raise RuntimeError("CoInitialize failed for C:\\secret\\job\\workbook.xlsx")

        pythoncom_mod.CoInitialize = failing_coinitialize
        pythoncom_mod.CoUninitialize = lambda: com_calls.__setitem__(
            "couninitialize",
            com_calls["couninitialize"] + 1,
        )
        win32com_mod = types.ModuleType("win32com")
        win32com_mod.__path__ = []
        win32com_client_mod = types.ModuleType("win32com.client")
        win32com_client_mod.DispatchEx = lambda prog_id: (_ for _ in ()).throw(
            RuntimeError("DispatchEx should not run after CoInitialize failure")
        )
        saved_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        sys.modules["pythoncom"] = pythoncom_mod
        sys.modules["win32com"] = win32com_mod
        sys.modules["win32com.client"] = win32com_client_mod
        try:
            rc, msg = recalc_mod.recalc_spreadsheet(workbook_path)
        finally:
            for name, original_module in saved_modules.items():
                if original_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original_module
        _assert(rc == 1, f"CoInitialize failure should fail recalc: {rc}, {msg!r}")
        _assert("Excel recalc failed" in msg, f"CoInitialize failure lost context: {msg!r}")
        _assert("C:\\secret" not in msg, f"CoInitialize failure leaked path: {msg!r}")
        _assert(com_calls["couninitialize"] == 0, "CoUninitialize ran after failed CoInitialize")

    original_main_creation_time = windows_process_mod._process_creation_time
    original_main_powershell_creation_time = windows_process_mod._windows_powershell_creation_time
    original_main_pid_is_excel = windows_process_mod._pid_is_excel
    original_main_subprocess_run = windows_process_mod.subprocess.run
    pid_checks = {"count": 0}

    def fake_pid_is_excel(*, platform: Platform, pid: int) -> bool:
        pid_checks["count"] += 1
        return pid_checks["count"] == 1

    windows_process_mod._process_creation_time = lambda pid: None
    windows_process_mod._windows_powershell_creation_time = lambda pid: 123456
    windows_process_mod._pid_is_excel = fake_pid_is_excel
    windows_process_mod.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=0)
    try:
        killed = windows_process_mod._kill_excel_pid(
            platform=Platform.WINDOWS,
            pid=123,
            expected_creation_time=123456,
        )
    finally:
        windows_process_mod._process_creation_time = original_main_creation_time
        windows_process_mod._windows_powershell_creation_time = original_main_powershell_creation_time
        windows_process_mod._pid_is_excel = original_main_pid_is_excel
        windows_process_mod.subprocess.run = original_main_subprocess_run
    _assert(killed is True, "per-job Excel kill did not use PowerShell creation-time fallback")

    original_list_excel_pids = windows_process_mod._list_excel_pids
    original_new_excel_creation_time = windows_process_mod._process_creation_time
    original_new_excel_powershell_time = windows_process_mod._windows_powershell_creation_time
    original_kill_excel_pid = windows_process_mod._kill_excel_pid
    kill_calls: list[dict[str, object]] = []
    windows_process_mod._list_excel_pids = lambda platform: {100, 200}
    windows_process_mod._process_creation_time = lambda pid: None
    windows_process_mod._windows_powershell_creation_time = lambda pid: 555 if pid == 200 else None

    def fake_kill_excel_pid(*, platform: Platform, pid: int, expected_creation_time: int | None = None) -> bool:
        kill_calls.append({"pid": pid, "creation_time": expected_creation_time})
        return expected_creation_time == 555

    windows_process_mod._kill_excel_pid = fake_kill_excel_pid
    try:
        killed_new = windows_process_mod._kill_new_excel_processes(
            platform=Platform.WINDOWS,
            baseline_pids={100},
        )
    finally:
        windows_process_mod._list_excel_pids = original_list_excel_pids
        windows_process_mod._process_creation_time = original_new_excel_creation_time
        windows_process_mod._windows_powershell_creation_time = original_new_excel_powershell_time
        windows_process_mod._kill_excel_pid = original_kill_excel_pid
    _assert(killed_new == 1, f"new Excel fallback kill count changed: {killed_new}")
    _assert(kill_calls == [{"pid": 200, "creation_time": 555}], f"new Excel fallback missed: {kill_calls}")

    baseline_failure_kill_calls: list[int] = []
    windows_process_mod._list_excel_pids = lambda platform: {200}

    def record_baseline_failure_kill(*, platform: Platform, pid: int, expected_creation_time: int | None = None) -> bool:
        baseline_failure_kill_calls.append(pid)
        return True

    windows_process_mod._kill_excel_pid = record_baseline_failure_kill
    try:
        killed_without_baseline = windows_process_mod._kill_new_excel_processes(
            platform=Platform.WINDOWS,
            baseline_pids=None,
        )
    finally:
        windows_process_mod._list_excel_pids = original_list_excel_pids
        windows_process_mod._kill_excel_pid = original_kill_excel_pid
    _assert(killed_without_baseline == 0, "fallback kill ran after baseline PID listing failed")
    _assert(not baseline_failure_kill_calls, f"baseline failure killed Excel PIDs: {baseline_failure_kill_calls}")

    current_failure_kill_calls: list[int] = []
    windows_process_mod._list_excel_pids = lambda platform: None

    def record_current_failure_kill(*, platform: Platform, pid: int, expected_creation_time: int | None = None) -> bool:
        current_failure_kill_calls.append(pid)
        return True

    windows_process_mod._kill_excel_pid = record_current_failure_kill
    try:
        killed_without_current = windows_process_mod._kill_new_excel_processes(
            platform=Platform.WINDOWS,
            baseline_pids={100},
        )
    finally:
        windows_process_mod._list_excel_pids = original_list_excel_pids
        windows_process_mod._kill_excel_pid = original_kill_excel_pid
    _assert(killed_without_current == 0, "fallback kill ran after current PID listing failed")
    _assert(not current_failure_kill_calls, f"current listing failure killed Excel PIDs: {current_failure_kill_calls}")

    def noisy_success_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": True, "reward": 0.75, "msg": "comparison finished"}
        telemetry = {"event": "telemetry", "msg": "still running"}
        stdout = f"dependency banner\n{json.dumps(payload)}\n{json.dumps(telemetry)}\n".encode()
        return False, stdout, b""

    reward, msg = await _with_fake_worker(
        noisy_success_worker,
        lambda: manager_mod._compute_reward_via_worker(
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            platform=Platform.WINDOWS,
        ),
    )
    _assert(reward == 0.75, f"noisy worker reward was not parsed: {reward!r}")
    _assert(msg == "comparison finished", f"noisy worker msg was not preserved: {msg!r}")

    def extra_key_reward_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": True, "reward": 0.8, "msg": "extra accepted", "duration_ms": 12}
        return False, f"{json.dumps(payload)}\n".encode(), b""

    reward, msg = await _with_fake_worker(
        extra_key_reward_worker,
        lambda: manager_mod._compute_reward_via_worker(
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            platform=Platform.WINDOWS,
        ),
    )
    _assert(reward == 0.8, f"extra-key reward payload was not parsed: {reward!r}")
    _assert(msg == "extra accepted", f"extra-key reward msg changed: {msg!r}")

    long_failure = "worker error:\n" + ("x" * 600)

    def failing_reward_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": False, "reward": 0.0, "msg": long_failure}
        return False, f"{json.dumps(payload)}\n".encode(), b""

    try:
        await _with_fake_worker(
            failing_reward_worker,
            lambda: manager_mod._compute_reward_via_worker(
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                platform=Platform.WINDOWS,
            ),
        )
    except RuntimeError as exc:
        error_text = str(exc)
    else:
        raise AssertionError("reward worker failure did not raise")
    _assert("worker error:" in error_text, f"worker failure detail was lost: {error_text!r}")
    _assert("\n" not in error_text, f"worker failure detail was not normalized: {error_text!r}")
    _assert(len(error_text) <= 500, f"worker failure detail was not capped: {len(error_text)}")

    path_failure = "worker error: failed to open C:\\secret\\job\\output.xlsx"

    def path_failing_reward_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": False, "reward": 0.0, "msg": path_failure}
        return False, f"{json.dumps(payload)}\n".encode(), b""

    try:
        await _with_fake_worker(
            path_failing_reward_worker,
            lambda: manager_mod._compute_reward_via_worker(
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                platform=Platform.WINDOWS,
            ),
        )
    except RuntimeError as exc:
        path_error_text = str(exc)
    else:
        raise AssertionError("path-bearing reward worker failure did not raise")
    _assert("C:\\secret" not in path_error_text, f"worker failure leaked path: {path_error_text!r}")

    def failing_recalc_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": False, "msg": "open failed: workbook locked"}
        return False, f"noise\n{json.dumps(payload)}\n".encode(), b""

    ok, recalc_msg = await _with_fake_worker(
        failing_recalc_worker,
        lambda: manager_mod._recalc_file_via_worker(
            proc_file=Path("proc.xlsx"),
            platform=Platform.WINDOWS,
        ),
    )
    _assert(ok is False, "recalc worker failure should return ok=False")
    _assert(
        recalc_msg == "open failed: workbook locked",
        f"recalc worker failure detail was lost: {recalc_msg!r}",
    )

    def noisy_recalc_success_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": True, "msg": "recalculated"}
        telemetry = {"event": "telemetry", "msg": "still running"}
        return False, f"{json.dumps(payload)}\n{json.dumps(telemetry)}\n".encode(), b""

    ok, recalc_msg = await _with_fake_worker(
        noisy_recalc_success_worker,
        lambda: manager_mod._recalc_file_via_worker(
            proc_file=Path("proc.xlsx"),
            platform=Platform.WINDOWS,
        ),
    )
    _assert(ok is True, "recalc worker valid result should be selected before trailing telemetry")
    _assert(recalc_msg == "recalculated", f"recalc worker msg was not preserved: {recalc_msg!r}")

    def extra_key_recalc_success_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": True, "msg": "extra recalculated", "duration_ms": 5}
        return False, f"{json.dumps(payload)}\n".encode(), b""

    ok, recalc_msg = await _with_fake_worker(
        extra_key_recalc_success_worker,
        lambda: manager_mod._recalc_file_via_worker(
            proc_file=Path("proc.xlsx"),
            platform=Platform.WINDOWS,
        ),
    )
    _assert(ok is True, "extra-key recalc payload was not parsed")
    _assert(recalc_msg == "extra recalculated", f"extra-key recalc msg changed: {recalc_msg!r}")

    original_kill_excel_pid_from_file = windows_process_mod._kill_excel_pid_from_file
    original_main_kill_excel_pid_from_file = manager_mod._kill_excel_pid_from_file
    unexpected_failure_kill_calls: list[tuple[Platform, Path]] = []
    worker_pid_file_args: list[Path | None] = []

    def record_kill_excel_pid_from_file(*, platform: Platform, pid_file: Path) -> bool:
        unexpected_failure_kill_calls.append((platform, pid_file))
        return True

    def record_worker_pid_file_arg(cmd: list[str]) -> None:
        try:
            idx = cmd.index("--excel-pid-file")
        except ValueError:
            worker_pid_file_args.append(None)
            return
        try:
            worker_pid_file_args.append(Path(cmd[idx + 1]))
        except IndexError:
            worker_pid_file_args.append(None)

    def unexpected_worker_failure(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        record_worker_pid_file_arg(cmd)
        raise RuntimeError("communicate failed")

    windows_process_mod._kill_excel_pid_from_file = record_kill_excel_pid_from_file
    manager_mod._kill_excel_pid_from_file = record_kill_excel_pid_from_file
    try:
        try:
            await _with_fake_worker(
                unexpected_worker_failure,
                lambda: manager_mod._compute_reward_via_worker(
                    gt_file=Path("gt.xlsx"),
                    proc_file=Path("proc.xlsx"),
                    answer_position="Sheet1!A1",
                    platform=Platform.WINDOWS,
                ),
            )
        except RuntimeError as exc:
            _assert(str(exc) == "communicate failed", f"unexpected reward failure changed: {exc}")
        else:
            raise AssertionError("unexpected reward worker failure did not propagate")

        try:
            await _with_fake_worker(
                unexpected_worker_failure,
                lambda: manager_mod._recalc_file_via_worker(
                    proc_file=Path("proc.xlsx"),
                    platform=Platform.WINDOWS,
                ),
            )
        except RuntimeError as exc:
            _assert(str(exc) == "communicate failed", f"unexpected recalc failure changed: {exc}")
        else:
            raise AssertionError("unexpected recalc worker failure did not propagate")

        def fatal_recalc_worker_exit(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
            record_worker_pid_file_arg(cmd)
            raise manager_mod.ControlledWorkerExitedError(
                "worker exited with returncode=2; stderr: [worker] fatal recalc failed: "
                "Excel recalc fatal failure: close failed"
            )

        try:
            await _with_fake_worker(
                fatal_recalc_worker_exit,
                lambda: manager_mod._recalc_file_via_worker(
                    proc_file=Path("proc.xlsx"),
                    platform=Platform.WINDOWS,
                ),
            )
        except RuntimeError as exc:
            _assert("fatal recalc failed" in str(exc), f"fatal recalc failure detail was lost: {exc}")
            _assert("close failed" in str(exc), f"fatal recalc terminal detail was lost: {exc}")
        else:
            raise AssertionError("fatal recalc worker exit did not propagate")

        def timeout_recalc_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
            record_worker_pid_file_arg(cmd)
            return True, b"", b""

        ok, timeout_msg = await _with_fake_worker(
            timeout_recalc_worker,
            lambda: manager_mod._recalc_file_via_worker(
                proc_file=Path("proc.xlsx"),
                platform=Platform.WINDOWS,
            ),
        )
        _assert(ok is False, "timeout recalc worker should return ok=False")
        _assert("timeout after" in timeout_msg, f"timeout recalc message changed: {timeout_msg!r}")

        def timeout_reward_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
            record_worker_pid_file_arg(cmd)
            return True, b"", b""

        try:
            await _with_fake_worker(
                timeout_reward_worker,
                lambda: manager_mod._compute_reward_via_worker(
                    gt_file=Path("gt.xlsx"),
                    proc_file=Path("proc.xlsx"),
                    answer_position="Sheet1!A1",
                    platform=Platform.WINDOWS,
                ),
            )
        except RuntimeError as exc:
            _assert("timeout after" in str(exc), f"timeout reward message changed: {exc}")
        else:
            raise AssertionError("timeout reward worker did not raise")
    finally:
        windows_process_mod._kill_excel_pid_from_file = original_kill_excel_pid_from_file
        manager_mod._kill_excel_pid_from_file = original_main_kill_excel_pid_from_file
    _assert(
        [platform for platform, _ in unexpected_failure_kill_calls]
        == [
            Platform.WINDOWS,
            Platform.WINDOWS,
            Platform.WINDOWS,
            Platform.WINDOWS,
            Platform.WINDOWS,
        ],
        f"unexpected worker failures did not attempt attributed Excel cleanup: {unexpected_failure_kill_calls}",
    )
    cleanup_pid_files = [pid_file for _, pid_file in unexpected_failure_kill_calls]
    _assert(
        len(set(cleanup_pid_files)) == len(cleanup_pid_files),
        f"unexpected worker failures reused cleanup pid files: {cleanup_pid_files}",
    )
    _assert(
        worker_pid_file_args == cleanup_pid_files,
        f"worker pid-file args did not match cleanup pid files: args={worker_pid_file_args}, cleanup={cleanup_pid_files}",
    )

    original_cleanup_worker_excel_pid_file = manager_mod._cleanup_worker_excel_pid_file
    original_run_worker_subprocess = manager_mod._run_worker_subprocess
    original_enable_timeout_excel_fallback_kill = manager_mod._enable_timeout_excel_fallback_kill
    original_list_excel_pids = manager_mod._list_excel_pids
    worker_exit_cleanup_calls: list[dict[str, object]] = []

    async def record_worker_exit_cleanup(
        *,
        platform: Platform,
        pid_file: Path,
        use_fallback_excel_kill: bool,
        baseline_excel_pids: set[int] | None,
    ):
        worker_exit_cleanup_calls.append(
            {
                "platform": platform,
                "use_fallback_excel_kill": use_fallback_excel_kill,
                "baseline_excel_pids": baseline_excel_pids,
            }
        )
        return False, 0

    async def controlled_worker_exited_subprocess(*, cmd: list[str], timeout_s: float):
        raise manager_mod.ControlledWorkerExitedError(
            "worker exited with returncode=2; stderr: [worker] fatal recalc failed: close failed"
        )

    manager_mod._cleanup_worker_excel_pid_file = record_worker_exit_cleanup
    manager_mod._run_worker_subprocess = controlled_worker_exited_subprocess
    manager_mod._enable_timeout_excel_fallback_kill = lambda: True
    manager_mod._list_excel_pids = lambda platform: {999}
    try:
        for label, worker_call in (
            (
                "reward",
                lambda: manager_mod._compute_reward_via_worker(
                    gt_file=Path("gt.xlsx"),
                    proc_file=Path("proc.xlsx"),
                    answer_position="Sheet1!A1",
                    platform=Platform.WINDOWS,
                ),
            ),
            (
                "recalc",
                lambda: manager_mod._recalc_file_via_worker(
                    proc_file=Path("proc.xlsx"),
                    platform=Platform.WINDOWS,
                ),
            ),
        ):
            try:
                await worker_call()
            except manager_mod.WorkerExitedError:
                pass
            else:
                raise AssertionError(f"{label} WorkerExitedError did not propagate")
    finally:
        manager_mod._cleanup_worker_excel_pid_file = original_cleanup_worker_excel_pid_file
        manager_mod._run_worker_subprocess = original_run_worker_subprocess
        manager_mod._enable_timeout_excel_fallback_kill = original_enable_timeout_excel_fallback_kill
        manager_mod._list_excel_pids = original_list_excel_pids
    _assert(
        [call["use_fallback_excel_kill"] for call in worker_exit_cleanup_calls] == [False, False],
        f"controlled worker exits used fallback cleanup: {worker_exit_cleanup_calls}",
    )
    _assert(
        [call["baseline_excel_pids"] for call in worker_exit_cleanup_calls] == [{999}, {999}],
        f"controlled worker exit cleanup lost baseline context: {worker_exit_cleanup_calls}",
    )

    worker_exit_cleanup_calls.clear()

    async def generic_worker_exited_subprocess(*, cmd: list[str], timeout_s: float):
        raise manager_mod.WorkerExitedError(
            "worker exited with returncode=1; stderr: worker import failed"
        )

    manager_mod._cleanup_worker_excel_pid_file = record_worker_exit_cleanup
    manager_mod._run_worker_subprocess = generic_worker_exited_subprocess
    manager_mod._enable_timeout_excel_fallback_kill = lambda: True
    manager_mod._list_excel_pids = lambda platform: {999}
    try:
        for label, worker_call in (
            (
                "reward",
                lambda: manager_mod._compute_reward_via_worker(
                    gt_file=Path("gt.xlsx"),
                    proc_file=Path("proc.xlsx"),
                    answer_position="Sheet1!A1",
                    platform=Platform.WINDOWS,
                ),
            ),
            (
                "recalc",
                lambda: manager_mod._recalc_file_via_worker(
                    proc_file=Path("proc.xlsx"),
                    platform=Platform.WINDOWS,
                ),
            ),
        ):
            try:
                await worker_call()
            except manager_mod.WorkerExitedError:
                pass
            else:
                raise AssertionError(f"{label} generic WorkerExitedError did not propagate")
    finally:
        manager_mod._cleanup_worker_excel_pid_file = original_cleanup_worker_excel_pid_file
        manager_mod._run_worker_subprocess = original_run_worker_subprocess
        manager_mod._enable_timeout_excel_fallback_kill = original_enable_timeout_excel_fallback_kill
        manager_mod._list_excel_pids = original_list_excel_pids
    _assert(
        [call["use_fallback_excel_kill"] for call in worker_exit_cleanup_calls] == [True, True],
        f"generic worker exits skipped fallback cleanup: {worker_exit_cleanup_calls}",
    )
    _assert(
        [call["baseline_excel_pids"] for call in worker_exit_cleanup_calls] == [{999}, {999}],
        f"generic worker exit cleanup lost baseline context: {worker_exit_cleanup_calls}",
    )

    controlled_cancel_cleanup_calls: list[tuple[str, bool]] = []

    async def controlled_exit_for_cancel(*, cmd: list[str], timeout_s: float):
        raise manager_mod.ControlledWorkerExitedError(
            "worker exited with returncode=2; stderr: [worker] fatal recalc failed: close failed"
        )

    for label, worker_call in (
        (
            "reward",
            lambda: manager_mod._compute_reward_via_worker(
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                platform=Platform.WINDOWS,
            ),
        ),
        (
            "recalc",
            lambda: manager_mod._recalc_file_via_worker(
                proc_file=Path("proc.xlsx"),
                platform=Platform.WINDOWS,
            ),
        ),
    ):
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleanup_call_count = 0

        async def blocking_controlled_exit_cleanup(
            *,
            platform: Platform,
            pid_file: Path,
            use_fallback_excel_kill: bool,
            baseline_excel_pids: set[int] | None,
        ):
            nonlocal cleanup_call_count
            cleanup_call_count += 1
            controlled_cancel_cleanup_calls.append((label, use_fallback_excel_kill))
            if cleanup_call_count == 1:
                cleanup_started.set()
                await cleanup_release.wait()
            return False, 0

        manager_mod._cleanup_worker_excel_pid_file = blocking_controlled_exit_cleanup
        manager_mod._run_worker_subprocess = controlled_exit_for_cancel
        manager_mod._enable_timeout_excel_fallback_kill = lambda: True
        manager_mod._list_excel_pids = lambda platform: {999}
        try:
            task = asyncio.create_task(worker_call())
            await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
            task.cancel()
            await asyncio.sleep(0)
            cleanup_release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError(f"cancelled controlled {label} worker exit did not raise CancelledError")
        finally:
            cleanup_release.set()
            manager_mod._cleanup_worker_excel_pid_file = original_cleanup_worker_excel_pid_file
            manager_mod._run_worker_subprocess = original_run_worker_subprocess
            manager_mod._enable_timeout_excel_fallback_kill = original_enable_timeout_excel_fallback_kill
            manager_mod._list_excel_pids = original_list_excel_pids
    _assert(
        controlled_cancel_cleanup_calls
        == [("reward", False), ("reward", False), ("recalc", False), ("recalc", False)],
        f"controlled exit cancellation re-enabled fallback cleanup: {controlled_cancel_cleanup_calls}",
    )

    original_cleanup_worker_excel_pid_file = manager_mod._cleanup_worker_excel_pid_file
    original_unlink_missing_ok = manager_mod._unlink_missing_ok
    original_run_worker_subprocess = manager_mod._run_worker_subprocess
    cleanup_calls: list[str] = []
    unlink_calls: list[Path] = []

    async def hanging_worker_subprocess(*, cmd: list[str], timeout_s: float):
        await asyncio.Event().wait()

    async def blocking_cleanup_worker_excel_pid_file(
        *,
        platform: Platform,
        pid_file: Path,
        use_fallback_excel_kill: bool,
        baseline_excel_pids: set[int],
    ):
        cleanup_calls.append(str(pid_file))
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_finished.set()
        return False, 0

    def recording_unlink_missing_ok(path: Path) -> None:
        unlink_calls.append(path)
        return None

    for label, worker_call in (
        (
            "reward",
            lambda: manager_mod._compute_reward_via_worker(
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                platform=Platform.WINDOWS,
            ),
        ),
        (
            "recalc",
            lambda: manager_mod._recalc_file_via_worker(
                proc_file=Path("proc.xlsx"),
                platform=Platform.WINDOWS,
            ),
        ),
    ):
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleanup_finished = asyncio.Event()
        manager_mod._run_worker_subprocess = hanging_worker_subprocess
        manager_mod._cleanup_worker_excel_pid_file = blocking_cleanup_worker_excel_pid_file
        manager_mod._unlink_missing_ok = recording_unlink_missing_ok
        try:
            task = asyncio.create_task(worker_call())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
            task.cancel()
            await asyncio.sleep(0)
            cleanup_release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError(f"cancelled {label} worker did not raise CancelledError")
            _assert(cleanup_finished.is_set(), f"double-cancelled {label} worker cleanup did not finish")
        finally:
            cleanup_release.set()
            manager_mod._run_worker_subprocess = original_run_worker_subprocess
            manager_mod._cleanup_worker_excel_pid_file = original_cleanup_worker_excel_pid_file
            manager_mod._unlink_missing_ok = original_unlink_missing_ok
    _assert(len(cleanup_calls) == 2, f"worker cancellation cleanup call count changed: {cleanup_calls}")
    _assert(len(unlink_calls) == 2, f"worker cancellation pid file unlink count changed: {unlink_calls}")

    original_recalc_spreadsheet = worker_mod._recalc_spreadsheet
    worker_main_pid_files: list[Path | None] = []

    def failing_worker_recalc(platform: Platform, file_path: Path, *, excel_pid_file=None):
        worker_main_pid_files.append(excel_pid_file)
        return 1, "Excel recalc failed:\nopen failed"

    worker_mod._recalc_spreadsheet = failing_worker_recalc
    try:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = worker_mod.main(
                [
                    "--platform",
                    "windows",
                    "--proc-file",
                    "missing.xlsx",
                    "--recalc-only",
                ]
            )
        _assert(rc == 0, f"worker main returned nonzero: {rc}")
        payload = json.loads(stdout.getvalue())
        _assert(payload.get("ok") is False, f"worker recalc failure should be ok=false: {payload}")
        _assert(
            payload.get("msg") == "Excel recalc failed: open failed",
            f"worker recalc failure detail was not preserved: {payload}",
        )
        _assert(worker_main_pid_files[-1] is None, "worker main unexpectedly set pid file without CLI arg")

        def path_failing_worker_recalc(platform: Platform, file_path: Path, *, excel_pid_file=None):
            worker_main_pid_files.append(excel_pid_file)
            return 1, "Excel recalc failed: open failed for C:\\secret\\job\\workbook.xlsx"

        worker_mod._recalc_spreadsheet = path_failing_worker_recalc
        cli_pid_file = Path("worker_pid_file.json")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = worker_mod.main(
                [
                    "--platform",
                    "windows",
                    "--proc-file",
                    "missing.xlsx",
                    "--recalc-only",
                    "--excel-pid-file",
                    str(cli_pid_file),
                ]
            )
        _assert(rc == 0, f"worker main returned nonzero: {rc}")
        payload = json.loads(stdout.getvalue())
        _assert(payload.get("ok") is False, f"worker recalc failure should be ok=false: {payload}")
        _assert("C:\\secret" not in str(payload.get("msg")), f"worker recalc failure leaked path: {payload}")
        _assert(worker_main_pid_files[-1] == cli_pid_file, "worker main did not pass CLI pid file to recalc")

        def fatal_worker_recalc(platform: Platform, file_path: Path, *, excel_pid_file=None):
            worker_main_pid_files.append(excel_pid_file)
            return 2, "Excel recalc fatal failure: close failed"

        worker_mod._recalc_spreadsheet = fatal_worker_recalc
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = worker_mod.main(
                [
                    "--platform",
                    "windows",
                    "--proc-file",
                    "missing.xlsx",
                    "--recalc-only",
                ]
            )
        _assert(rc == 2, f"fatal worker recalc should exit nonzero: {rc}")
        _assert(stdout.getvalue() == "", f"fatal worker recalc emitted JSON stdout: {stdout.getvalue()!r}")
        _assert("fatal recalc failed" in stderr.getvalue(), "fatal worker recalc did not log to stderr")
        _assert("close failed" in stderr.getvalue(), "fatal worker recalc terminal detail was not logged")
        _assert(worker_main_pid_files[-1] is None, "fatal worker main unexpectedly set pid file without CLI arg")
    finally:
        worker_mod._recalc_spreadsheet = original_recalc_spreadsheet

    def missing_reward_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": True, "msg": "missing reward"}
        return False, f"{json.dumps(payload)}\n".encode(), b""

    try:
        await _with_fake_worker(
            missing_reward_worker,
            lambda: manager_mod._compute_reward_via_worker(
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                platform=Platform.WINDOWS,
            ),
        )
    except RuntimeError as exc:
        _assert(str(exc) == "missing worker reward", f"unexpected missing reward error: {exc}")
    else:
        raise AssertionError("missing reward payload did not fail")

    def non_finite_reward_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": True, "reward": float("inf"), "msg": "bad reward"}
        return False, f"{json.dumps(payload)}\n".encode(), b""

    try:
        await _with_fake_worker(
            non_finite_reward_worker,
            lambda: manager_mod._compute_reward_via_worker(
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                platform=Platform.WINDOWS,
            ),
        )
    except RuntimeError as exc:
        _assert(str(exc) == "invalid worker reward", f"unexpected non-finite reward error: {exc}")
    else:
        raise AssertionError("non-finite reward payload did not fail")

    def boolean_reward_worker(*, cmd: list[str], timeout_s: float) -> tuple[bool, bytes, bytes]:
        payload = {"ok": True, "reward": True, "msg": "bad reward"}
        return False, f"{json.dumps(payload)}\n".encode(), b""

    try:
        await _with_fake_worker(
            boolean_reward_worker,
            lambda: manager_mod._compute_reward_via_worker(
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                platform=Platform.WINDOWS,
            ),
        )
    except RuntimeError as exc:
        _assert(str(exc) == "invalid worker reward", f"unexpected boolean reward error: {exc}")
    else:
        raise AssertionError("boolean reward payload did not fail")

    pooled_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    pooled_worker._proc = _FakeProc()
    pooled_worker._rx.put_nowait(
        {
            "type": "result",
            "job_id": "bad-ok",
            "ok": "false",
            "reward": 1.0,
            "msg": "bad protocol",
        }
    )
    try:
        await pooled_worker.run_job(
            job_id="bad-ok",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "invalid worker ok", f"unexpected pooled ok error: {exc}")
    else:
        raise AssertionError("pooled non-boolean ok did not fail")

    pooled_recalc_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    pooled_recalc_worker._proc = _FakeProc()
    pooled_recalc_worker._rx.put_nowait(
        {
            "type": "result",
            "job_id": "bad-recalc-ok",
            "ok": "false",
            "msg": "bad protocol",
        }
    )
    try:
        await pooled_recalc_worker.run_recalc(
            job_id="bad-recalc-ok",
            proc_file=Path("proc.xlsx"),
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "invalid worker ok", f"unexpected pooled recalc ok error: {exc}")
    else:
        raise AssertionError("pooled recalc non-boolean ok did not fail")

    raw_stdout_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    raw_stdout_proc = _FakeStartedProc()
    raw_stdout_proc.stdout = io.StringIO("dependency banner\n")
    raw_stdout_worker._proc = raw_stdout_proc
    raw_stdout_worker._stdout_loop()
    await asyncio.sleep(0)
    try:
        await raw_stdout_worker.run_job(
            job_id="raw-stdout",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "invalid worker stdout", f"unexpected raw stdout error: {exc}")
    else:
        raise AssertionError("pooled raw stdout contamination did not fail fast")

    closed_loop_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    closed_loop_worker._loop = _ClosedLoop()
    closed_loop_proc = _FakeStartedProc()
    closed_loop_proc.stdout = io.StringIO("dependency banner\n")
    closed_loop_worker._proc = closed_loop_proc
    closed_loop_worker._stdout_loop()

    pooled_missing_reward = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    pooled_missing_reward._proc = _FakeProc()
    pooled_missing_reward._rx.put_nowait(
        {
            "type": "result",
            "job_id": "missing-reward",
            "ok": True,
            "msg": "missing reward",
        }
    )
    try:
        await pooled_missing_reward.run_job(
            job_id="missing-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "missing worker reward", f"unexpected pooled reward error: {exc}")
    else:
        raise AssertionError("pooled missing reward did not fail")

    pooled_non_finite_reward = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    pooled_non_finite_reward._proc = _FakeProc()
    pooled_non_finite_reward._rx.put_nowait(
        {
            "type": "result",
            "job_id": "non-finite-reward",
            "ok": True,
            "reward": float("nan"),
            "msg": "bad reward",
        }
    )
    try:
        await pooled_non_finite_reward.run_job(
            job_id="non-finite-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "invalid worker reward", f"unexpected pooled non-finite error: {exc}")
    else:
        raise AssertionError("pooled non-finite reward did not fail")

    pooled_boolean_reward = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    pooled_boolean_reward._proc = _FakeProc()
    pooled_boolean_reward._rx.put_nowait(
        {
            "type": "result",
            "job_id": "boolean-reward",
            "ok": True,
            "reward": True,
            "msg": "bad reward",
        }
    )
    try:
        await pooled_boolean_reward.run_job(
            job_id="boolean-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "invalid worker reward", f"unexpected pooled boolean reward error: {exc}")
    else:
        raise AssertionError("pooled boolean reward did not fail")

    pooled_wrong_job_id = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    pooled_wrong_job_id._proc = _FakeProc()
    pooled_wrong_job_id._rx.put_nowait(
        {
            "type": "result",
            "job_id": "other-reward",
            "ok": True,
            "reward": 1.0,
            "msg": "wrong job",
        }
    )
    try:
        await pooled_wrong_job_id.run_job(
            job_id="expected-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "unexpected worker job_id", f"unexpected pooled job-id error: {exc}")
    else:
        raise AssertionError("pooled wrong reward job_id did not fail")

    pooled_wrong_recalc_job_id = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
    pooled_wrong_recalc_job_id._proc = _FakeProc()
    pooled_wrong_recalc_job_id._rx.put_nowait(
        {
            "type": "result",
            "job_id": "other-recalc",
            "ok": True,
            "msg": "wrong job",
        }
    )
    try:
        await pooled_wrong_recalc_job_id.run_recalc(
            job_id="expected-recalc",
            proc_file=Path("proc.xlsx"),
            timeout_s=1.0,
        )
    except WorkerProtocolError as exc:
        _assert(str(exc) == "unexpected worker job_id", f"unexpected pooled recalc job-id error: {exc}")
    else:
        raise AssertionError("pooled wrong recalc job_id did not fail")

    pool = ExcelWorkerPool(size=1, platform="windows")
    pool._closing = True
    protocol_worker = _ProtocolErrorWorker("missing worker reward")
    pool._workers = [protocol_worker]
    try:
        await pool.run_job_with_worker(
            protocol_worker,
            job_id="protocol-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except RuntimeError as exc:
        _assert(
            str(exc) == "worker protocol error: missing worker reward",
            f"pooled protocol detail was not preserved: {exc}",
        )
    else:
        raise AssertionError("pooled protocol error did not fail")
    _assert(protocol_worker.shutdown_called, "failed pooled worker was not shut down")

    recalc_pool = ExcelWorkerPool(size=1, platform="windows")
    recalc_pool._closing = True
    protocol_recalc_worker = _ProtocolErrorWorker("invalid worker ok")
    recalc_pool._workers = [protocol_recalc_worker]
    ok, msg = await recalc_pool.recalc_file_with_worker(
        protocol_recalc_worker,
        job_id="protocol-recalc",
        proc_file=Path("proc.xlsx"),
        timeout_s=1.0,
    )
    _assert(ok is False, "pooled recalc protocol error should return ok=False")
    _assert(
        msg == "worker protocol error: invalid worker ok",
        f"pooled recalc protocol detail was not preserved: {msg!r}",
    )

    died_pool = ExcelWorkerPool(size=1, platform="windows")
    died_pool._closing = True
    died_worker = _DiedPoolWorker("worker exited (returncode=1); stderr_tail: C:\\secret\\job\\output.xlsx")
    died_pool._workers = [died_worker]
    try:
        await died_pool.run_job_with_worker(
            died_worker,
            job_id="died-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except RuntimeError as exc:
        died_msg = str(exc)
        _assert("worker died:" in died_msg, f"pooled worker death detail was not preserved: {died_msg!r}")
        _assert("returncode=1" in died_msg, f"pooled worker death return code was lost: {died_msg!r}")
        _assert("C:\\secret" not in died_msg, f"pooled worker death leaked path: {died_msg!r}")
    else:
        raise AssertionError("pooled worker death did not fail")
    _assert(died_worker.shutdown_called, "dead pooled reward worker was not shut down")

    recalc_died_pool = ExcelWorkerPool(size=1, platform="windows")
    recalc_died_pool._closing = True
    recalc_died_worker = _DiedPoolWorker(
        "worker exited (returncode=2); stderr_tail: C:\\secret\\job\\workbook.xlsx"
    )
    recalc_died_pool._workers = [recalc_died_worker]
    ok, died_recalc_msg = await recalc_died_pool.recalc_file_with_worker(
        recalc_died_worker,
        job_id="died-recalc",
        proc_file=Path("proc.xlsx"),
        timeout_s=1.0,
    )
    _assert(ok is False, "pooled recalc worker death should return ok=False")
    _assert("worker died:" in died_recalc_msg, f"pooled recalc death detail was not preserved: {died_recalc_msg!r}")
    _assert("returncode=2" in died_recalc_msg, f"pooled recalc death return code was lost: {died_recalc_msg!r}")
    _assert("C:\\secret" not in died_recalc_msg, f"pooled recalc death leaked path: {died_recalc_msg!r}")
    _assert(recalc_died_worker.shutdown_called, "dead pooled recalc worker was not shut down")

    success_msg = "completed " + ("x" * 600) + " C:\\secret\\job\\output.xlsx"
    success_pool = ExcelWorkerPool(size=1, platform="windows")
    success_worker = _SuccessfulPoolWorker(msg=success_msg, excel_pid=None)
    success_pool._workers = [success_worker]
    pooled_reward, pooled_msg = await success_pool.run_job_with_worker(
        success_worker,
        job_id="success-reward",
        gt_file=Path("gt.xlsx"),
        proc_file=Path("proc.xlsx"),
        answer_position="Sheet1!A1",
        timeout_s=1.0,
    )
    _assert(pooled_reward == 1.0, f"pooled success reward changed: {pooled_reward!r}")
    _assert(len(pooled_msg) <= 500, f"pooled success msg was not capped: {len(pooled_msg)}")
    _assert("C:\\secret" not in pooled_msg, f"pooled success msg leaked path: {pooled_msg!r}")

    failing_pool = ExcelWorkerPool(size=1, platform="windows")
    failing_worker = _FailingPoolWorker(msg="bad workbook")
    failing_pool._workers = [failing_worker]
    try:
        await failing_pool.run_job_with_worker(
            failing_worker,
            job_id="failing-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    except RuntimeError as exc:
        _assert(str(exc) == "bad workbook", f"pooled job failure detail changed: {exc}")
    else:
        raise AssertionError("pooled job-level failure did not fail")
    _assert(not failing_worker.shutdown_called, "job-level failure recycled a healthy pooled worker")
    _assert(failing_pool._workers == [failing_worker], "job-level failure swapped pooled worker")
    _assert(failing_pool._available.qsize() == 1, "job-level failure did not requeue pooled worker")

    fatal_pool = ExcelWorkerPool(size=1, platform="windows")
    fatal_worker = _FailingPoolWorker(msg="fatal worker error: close failed")
    fatal_pool._workers = [fatal_worker]
    replacement_workers: list[_ReplacementPoolWorker] = []
    replacement_start = asyncio.Event()
    replacement_release = asyncio.Event()

    class _RecordingReplacementPoolWorker(_ReplacementPoolWorker):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            replacement_workers.append(self)

        async def start(self) -> None:
            self.start_called = True
            replacement_start.set()
            await replacement_release.wait()

    original_excel_worker_process_cls = excel_pool_mod.ExcelWorkerProcess
    excel_pool_mod.ExcelWorkerProcess = _RecordingReplacementPoolWorker
    try:
        try:
            await asyncio.wait_for(
                fatal_pool.run_job_with_worker(
                    fatal_worker,
                    job_id="fatal-reward",
                    gt_file=Path("gt.xlsx"),
                    proc_file=Path("proc.xlsx"),
                    answer_position="Sheet1!A1",
                    timeout_s=1.0,
                ),
                timeout=1.0,
            )
        except RuntimeError as exc:
            _assert(str(exc) == "fatal worker error: close failed", f"fatal pooled job detail changed: {exc}")
        else:
            raise AssertionError("fatal pooled job-level failure did not fail")
        _assert(fatal_worker.shutdown_called, "fatal pooled job failure did not shut down failed worker")
        _assert(fatal_pool._workers == [fatal_worker], "fatal pooled job swapped replacement before returning")
        _assert(fatal_pool._available.qsize() == 0, "fatal pooled job exposed replacement before startup")
        _assert(len(fatal_pool._restart_tasks) == 1, "fatal pooled job did not schedule background restart")
        restart_tasks = list(fatal_pool._restart_tasks.values())
        await asyncio.wait_for(replacement_start.wait(), timeout=1.0)
        _assert(len(replacement_workers) == 1, f"fatal pooled job did not create replacement: {replacement_workers}")
        _assert(replacement_workers[0].start_called, "fatal pooled job replacement was not started")
        replacement_release.set()
        await asyncio.gather(*restart_tasks)
    finally:
        replacement_release.set()
        await asyncio.gather(*list(fatal_pool._restart_tasks.values()), return_exceptions=True)
        excel_pool_mod.ExcelWorkerProcess = original_excel_worker_process_cls
    _assert(fatal_pool._workers == [replacement_workers[0]], "fatal pooled job did not swap replacement worker")
    _assert(fatal_pool._available.qsize() == 1, "fatal pooled job replacement was not requeued")
    _assert(
        fatal_pool._available.get_nowait() is replacement_workers[0],
        "fatal pooled job requeued the failed worker instead of replacement",
    )

    failing_recalc_pool = ExcelWorkerPool(size=1, platform="windows")
    failing_recalc_worker = _FailingPoolWorker(msg="bad recalc")
    failing_recalc_pool._workers = [failing_recalc_worker]
    ok, recalc_failure_msg = await failing_recalc_pool.recalc_file_with_worker(
        failing_recalc_worker,
        job_id="failing-recalc",
        proc_file=Path("proc.xlsx"),
        timeout_s=1.0,
    )
    _assert(ok is False, "pooled recalc job-level failure should return ok=False")
    _assert(recalc_failure_msg == "bad recalc", f"pooled recalc failure detail changed: {recalc_failure_msg}")
    _assert(not failing_recalc_worker.shutdown_called, "recalc job-level failure recycled a healthy pooled worker")
    _assert(failing_recalc_pool._workers == [failing_recalc_worker], "recalc job-level failure swapped worker")
    _assert(failing_recalc_pool._available.qsize() == 1, "recalc job-level failure did not requeue worker")

    fatal_recalc_pool = ExcelWorkerPool(size=1, platform="windows")
    fatal_recalc_worker = _FailingPoolWorker(msg="fatal worker error: close failed")
    fatal_recalc_pool._workers = [fatal_recalc_worker]
    recalc_replacement_workers: list[_ReplacementPoolWorker] = []
    recalc_replacement_start = asyncio.Event()
    recalc_replacement_release = asyncio.Event()

    class _RecordingRecalcReplacementPoolWorker(_ReplacementPoolWorker):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            recalc_replacement_workers.append(self)

        async def start(self) -> None:
            self.start_called = True
            recalc_replacement_start.set()
            await recalc_replacement_release.wait()

    original_excel_worker_process_cls = excel_pool_mod.ExcelWorkerProcess
    excel_pool_mod.ExcelWorkerProcess = _RecordingRecalcReplacementPoolWorker
    try:
        ok, fatal_recalc_msg = await asyncio.wait_for(
            fatal_recalc_pool.recalc_file_with_worker(
                fatal_recalc_worker,
                job_id="fatal-recalc",
                proc_file=Path("proc.xlsx"),
                timeout_s=1.0,
            ),
            timeout=1.0,
        )
        _assert(ok is False, "fatal pooled recalc failure should return ok=False")
        _assert(
            fatal_recalc_msg == "fatal worker error: close failed",
            f"fatal pooled recalc detail changed: {fatal_recalc_msg}",
        )
        _assert(fatal_recalc_worker.shutdown_called, "fatal pooled recalc did not shut down failed worker")
        _assert(
            fatal_recalc_pool._workers == [fatal_recalc_worker],
            "fatal pooled recalc swapped replacement before returning",
        )
        _assert(fatal_recalc_pool._available.qsize() == 0, "fatal pooled recalc exposed replacement before startup")
        _assert(
            len(fatal_recalc_pool._restart_tasks) == 1,
            "fatal pooled recalc did not schedule background restart",
        )
        restart_tasks = list(fatal_recalc_pool._restart_tasks.values())
        await asyncio.wait_for(recalc_replacement_start.wait(), timeout=1.0)
        recalc_replacement_release.set()
        await asyncio.gather(*restart_tasks)
    finally:
        recalc_replacement_release.set()
        await asyncio.gather(*list(fatal_recalc_pool._restart_tasks.values()), return_exceptions=True)
        excel_pool_mod.ExcelWorkerProcess = original_excel_worker_process_cls
    _assert(
        len(recalc_replacement_workers) == 1,
        f"fatal pooled recalc did not create replacement: {recalc_replacement_workers}",
    )
    _assert(recalc_replacement_workers[0].start_called, "fatal pooled recalc replacement was not started")
    _assert(
        fatal_recalc_pool._workers == [recalc_replacement_workers[0]],
        "fatal pooled recalc did not swap replacement worker",
    )
    _assert(fatal_recalc_pool._available.qsize() == 1, "fatal pooled recalc replacement was not requeued")
    _assert(
        fatal_recalc_pool._available.get_nowait() is recalc_replacement_workers[0],
        "fatal pooled recalc requeued the failed worker instead of replacement",
    )

    release_cancel_pool = ExcelWorkerPool(size=1, platform="windows")
    release_cancel_worker = _SuccessfulPoolWorker(msg="done", excel_pid=None)
    release_cancel_pool._workers = [release_cancel_worker]

    async def unexpected_release_health(worker, *, timeout_s: float) -> bool:
        raise AssertionError("successful reward release should use recent healthy cache")

    release_cancel_pool._worker_is_healthy_bounded = unexpected_release_health
    release_cancel_reward, release_cancel_msg = await release_cancel_pool.run_job_with_worker(
        release_cancel_worker,
        job_id="release-cancel-reward",
        gt_file=Path("gt.xlsx"),
        proc_file=Path("proc.xlsx"),
        answer_position="Sheet1!A1",
        timeout_s=1.0,
    )
    _assert(release_cancel_reward == 1.0, f"release cancellation lost completed reward: {release_cancel_reward!r}")
    _assert(release_cancel_msg == "done", f"release cancellation lost completed reward msg: {release_cancel_msg!r}")
    _assert(release_cancel_pool._available.qsize() == 1, "release cancellation did not return reward worker")

    recalc_release_cancel_pool = ExcelWorkerPool(size=1, platform="windows")
    recalc_release_cancel_worker = _SuccessfulPoolWorker(msg="recalculated", excel_pid=None)
    recalc_release_cancel_pool._workers = [recalc_release_cancel_worker]

    async def unexpected_recalc_release_health(worker, *, timeout_s: float) -> bool:
        raise AssertionError("successful recalc release should use recent healthy cache")

    recalc_release_cancel_pool._worker_is_healthy_bounded = unexpected_recalc_release_health
    recalc_release_cancel_ok, recalc_release_cancel_msg = await recalc_release_cancel_pool.recalc_file_with_worker(
        recalc_release_cancel_worker,
        job_id="release-cancel-recalc",
        proc_file=Path("proc.xlsx"),
        timeout_s=1.0,
    )
    _assert(recalc_release_cancel_ok is True, "release cancellation lost completed recalc result")
    _assert(
        recalc_release_cancel_msg == "recalculated",
        f"release cancellation lost completed recalc msg: {recalc_release_cancel_msg!r}",
    )
    _assert(recalc_release_cancel_pool._available.qsize() == 1, "release cancellation did not return recalc worker")

    release_probe_cancel_pool = ExcelWorkerPool(size=1, platform="windows")
    release_probe_cancel_worker = _SuccessfulPoolWorker(msg="done", excel_pid=None)
    release_probe_cancel_pool._workers = [release_probe_cancel_worker]
    release_probe_cancel_started = asyncio.Event()

    async def blocking_release_probe(worker, *, timeout_s: float) -> bool:
        release_probe_cancel_started.set()
        await asyncio.Event().wait()
        return True

    release_probe_cancel_pool._worker_recently_verified_healthy = lambda worker: False
    release_probe_cancel_pool._worker_is_healthy_bounded = blocking_release_probe
    release_probe_cancel_task = asyncio.create_task(
        release_probe_cancel_pool.run_job_with_worker(
            release_probe_cancel_worker,
            job_id="release-probe-cancel-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    )
    await asyncio.wait_for(release_probe_cancel_started.wait(), timeout=1.0)
    release_probe_cancel_task.cancel()
    release_probe_cancel_reward, release_probe_cancel_msg = await release_probe_cancel_task
    _assert(
        release_probe_cancel_reward == 1.0,
        f"release probe cancellation lost completed reward: {release_probe_cancel_reward!r}",
    )
    _assert(
        release_probe_cancel_msg == "done",
        f"release probe cancellation lost completed reward msg: {release_probe_cancel_msg!r}",
    )
    _assert(
        release_probe_cancel_pool._available.qsize() == 1,
        "release probe cancellation did not return reward worker",
    )
    _assert(
        release_probe_cancel_pool._available.get_nowait() is release_probe_cancel_worker,
        "release probe cancellation requeued the wrong reward worker",
    )

    recalc_release_probe_cancel_pool = ExcelWorkerPool(size=1, platform="windows")
    recalc_release_probe_cancel_worker = _SuccessfulPoolWorker(msg="recalculated", excel_pid=None)
    recalc_release_probe_cancel_pool._workers = [recalc_release_probe_cancel_worker]
    recalc_release_probe_cancel_started = asyncio.Event()

    async def blocking_recalc_release_probe(worker, *, timeout_s: float) -> bool:
        recalc_release_probe_cancel_started.set()
        await asyncio.Event().wait()
        return True

    recalc_release_probe_cancel_pool._worker_recently_verified_healthy = lambda worker: False
    recalc_release_probe_cancel_pool._worker_is_healthy_bounded = blocking_recalc_release_probe
    recalc_release_probe_cancel_task = asyncio.create_task(
        recalc_release_probe_cancel_pool.recalc_file_with_worker(
            recalc_release_probe_cancel_worker,
            job_id="release-probe-cancel-recalc",
            proc_file=Path("proc.xlsx"),
            timeout_s=1.0,
        )
    )
    await asyncio.wait_for(recalc_release_probe_cancel_started.wait(), timeout=1.0)
    recalc_release_probe_cancel_task.cancel()
    recalc_release_probe_cancel_ok, recalc_release_probe_cancel_msg = (
        await recalc_release_probe_cancel_task
    )
    _assert(recalc_release_probe_cancel_ok is True, "release probe cancellation lost completed recalc result")
    _assert(
        recalc_release_probe_cancel_msg == "recalculated",
        f"release probe cancellation lost completed recalc msg: {recalc_release_probe_cancel_msg!r}",
    )
    _assert(
        recalc_release_probe_cancel_pool._available.qsize() == 1,
        "release probe cancellation did not return recalc worker",
    )
    _assert(
        recalc_release_probe_cancel_pool._available.get_nowait() is recalc_release_probe_cancel_worker,
        "release probe cancellation requeued the wrong recalc worker",
    )

    dead_pool = ExcelWorkerPool(size=2, platform="windows")
    dead_worker = _IdlePoolWorker(is_running=False)
    live_worker = _IdlePoolWorker(is_running=True)
    dead_pool._workers = [dead_worker, live_worker]
    recycle_calls: list[tuple[int, object, str]] = []

    def record_dead_recycle(idx: int, *, expected, reason: str) -> None:
        recycle_calls.append((idx, expected, reason))

    dead_pool._schedule_recycle = record_dead_recycle
    dead_pool._available.put_nowait(dead_worker)
    dead_pool._available.put_nowait(live_worker)
    acquired = await dead_pool.acquire(timeout_s=1.0)
    _assert(acquired is live_worker, "dead idle worker was returned from acquire")
    _assert(
        recycle_calls == [(0, dead_worker, "worker was not running when acquired")],
        "dead worker was not scheduled for recycle",
    )

    release_dead_pool = ExcelWorkerPool(size=1, platform="windows")
    release_dead_worker = _IdlePoolWorker(is_running=False)
    release_dead_pool._workers = [release_dead_worker]
    release_recycle_calls: list[tuple[int, object, str]] = []

    def record_release_recycle(idx: int, *, expected, reason: str) -> None:
        release_recycle_calls.append((idx, expected, reason))

    release_dead_pool._schedule_recycle = record_release_recycle
    await release_dead_pool.release(release_dead_worker)
    _assert(release_dead_pool._available.qsize() == 0, "dead released worker was requeued")
    _assert(
        release_recycle_calls == [(0, release_dead_worker, "worker was not running when released")],
        "dead released worker was not scheduled for recycle",
    )
    orphan_acquire_pool = ExcelWorkerPool(size=1, platform="windows")
    orphan_acquire_worker = _IdlePoolWorker(is_running=True)
    orphan_acquire_pool._available.put_nowait(orphan_acquire_worker)
    try:
        await orphan_acquire_pool.acquire(timeout_s=0.01)
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("pool acquired worker that it did not own")

    orphan_release_pool = ExcelWorkerPool(size=1, platform="windows")
    orphan_release_worker = _IdlePoolWorker(is_running=True)
    await orphan_release_pool.release(orphan_release_worker)
    _assert(
        orphan_release_pool._available.qsize() == 0,
        "pool requeued worker that it did not own",
    )

    race_orphan_pool = ExcelWorkerPool(size=1, platform="windows")
    race_orphan_worker = _DelayedHealthWorker(delay_s=0.05, healthy=True)
    race_orphan_pool._workers = [race_orphan_worker]
    race_orphan_pool._available.put_nowait(race_orphan_worker)
    race_acquire_task = asyncio.create_task(race_orphan_pool.acquire(timeout_s=0.2))
    await asyncio.sleep(0.01)
    race_orphan_pool._workers.clear()
    try:
        await race_acquire_task
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("pool acquired worker orphaned during health probe")

    slow_health_pool = ExcelWorkerPool(size=1, platform="windows")
    slow_health_worker = _DelayedHealthWorker(delay_s=0.2, healthy=True)
    slow_health_pool._workers = [slow_health_worker]
    slow_health_pool._available.put_nowait(slow_health_worker)
    slow_health_recycle_calls: list[tuple[int, object, str]] = []

    def record_slow_health_recycle(idx: int, *, expected, reason: str) -> None:
        slow_health_recycle_calls.append((idx, expected, reason))

    slow_health_pool._schedule_recycle = record_slow_health_recycle
    acquire_started_at = asyncio.get_running_loop().time()
    try:
        await slow_health_pool.acquire(timeout_s=0.05)
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("slow health probe ignored acquire timeout")
    elapsed_s = asyncio.get_running_loop().time() - acquire_started_at
    _assert(elapsed_s < 0.18, f"acquire timeout waited for full health probe: {elapsed_s}")
    _assert(
        slow_health_recycle_calls == [(0, slow_health_worker, "worker health probe timed out when acquired")],
        f"slow acquire health probe did not schedule recycle: {slow_health_recycle_calls}",
    )

    cancel_acquire_pool = ExcelWorkerPool(size=1, platform="windows")
    cancel_acquire_worker = _DelayedHealthWorker(delay_s=0.2, healthy=True)
    cancel_acquire_pool._workers = [cancel_acquire_worker]
    cancel_acquire_pool._available.put_nowait(cancel_acquire_worker)
    cancel_acquire_task = asyncio.create_task(cancel_acquire_pool.acquire())
    await asyncio.sleep(0.01)
    cancel_acquire_task.cancel()
    try:
        await cancel_acquire_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled acquire did not raise CancelledError")
    _assert(cancel_acquire_pool._available.qsize() == 1, "cancelled acquire lost pool capacity")
    _assert(
        cancel_acquire_pool._available.get_nowait() is cancel_acquire_worker,
        "cancelled acquire requeued the wrong worker",
    )

    cancel_release_pool = ExcelWorkerPool(size=1, platform="windows")
    cancel_release_worker = _BlockingHealthWorker()
    cancel_release_pool._workers = [cancel_release_worker]
    cancel_release_task = asyncio.create_task(cancel_release_pool.release(cancel_release_worker))
    await asyncio.wait_for(cancel_release_worker.health_started.wait(), timeout=1.0)
    cancel_release_task.cancel()
    try:
        await cancel_release_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled release did not raise CancelledError")
    cancel_release_worker.health_release.set()
    _assert(cancel_release_pool._available.qsize() == 1, "cancelled release lost pool capacity")
    _assert(
        cancel_release_pool._available.get_nowait() is cancel_release_worker,
        "cancelled release requeued the wrong worker",
    )

    manager_cancel_release_pool = ExcelWorkerPool(size=1, platform="windows")
    manager_cancel_release_worker = _BlockingHealthWorker()
    manager_cancel_release_pool._workers = [manager_cancel_release_worker]
    manager_for_release = manager_mod.RewardJobManager(store=_CaptureStore(), platform=Platform.WINDOWS)
    manager_for_release._excel_pool = manager_cancel_release_pool
    manager_cancel_release_task = asyncio.create_task(
        manager_for_release._release_excel_worker_after_claim(
            manager_cancel_release_worker,
            context="cancelled release",
        )
    )
    await asyncio.wait_for(manager_cancel_release_worker.health_started.wait(), timeout=1.0)
    manager_cancel_release_task.cancel()
    manager_release_ok = await manager_cancel_release_task
    manager_cancel_release_worker.health_release.set()
    _assert(manager_release_ok is False, "manager cancelled release did not report cancellation")
    _assert(manager_cancel_release_pool._available.qsize() == 1, "manager cancelled release lost pool capacity")
    _assert(
        manager_cancel_release_pool._available.get_nowait() is manager_cancel_release_worker,
        "manager cancelled release requeued the wrong worker",
    )

    release_error_pool = ExcelWorkerPool(size=1, platform="windows")
    release_error_worker = _IdlePoolWorker(is_running=True)
    release_error_pool._workers = [release_error_worker]
    release_error_recycle_calls: list[tuple[int, object, str]] = []

    def record_release_error_recycle(idx: int, *, expected, reason: str) -> None:
        release_error_recycle_calls.append((idx, expected, reason))

    async def failing_release_health(worker, *, timeout_s: float) -> bool:
        raise RuntimeError("release boom")

    release_error_pool._schedule_recycle = record_release_error_recycle
    release_error_pool._worker_is_healthy_bounded = failing_release_health
    try:
        await release_error_pool.release(release_error_worker)
    except RuntimeError as exc:
        _assert(str(exc) == "release boom", f"release failure detail changed: {exc!r}")
    else:
        raise AssertionError("release health failure did not propagate")
    _assert(release_error_pool._available.qsize() == 0, "failed release requeued worker")
    _assert(
        release_error_recycle_calls == [(0, release_error_worker, "worker release failed")],
        f"failed release did not schedule recycle: {release_error_recycle_calls}",
    )

    manager_release_error_pool = ExcelWorkerPool(size=1, platform="windows")
    manager_release_error_worker = _IdlePoolWorker(is_running=True)
    manager_release_error_pool._workers = [manager_release_error_worker]
    manager_release_error_recycle_calls: list[tuple[int, object, str]] = []

    def record_manager_release_error_recycle(idx: int, *, expected, reason: str) -> None:
        manager_release_error_recycle_calls.append((idx, expected, reason))

    manager_release_error_pool._schedule_recycle = record_manager_release_error_recycle
    manager_release_error_pool._worker_is_healthy_bounded = failing_release_health
    manager_for_release_error = manager_mod.RewardJobManager(store=_CaptureStore(), platform=Platform.WINDOWS)
    manager_for_release_error._excel_pool = manager_release_error_pool
    manager_release_error_ok = await manager_for_release_error._release_excel_worker_after_claim(
        manager_release_error_worker,
        context="release failure",
    )
    _assert(manager_release_error_ok is True, "manager release failure did not return True")
    _assert(
        manager_for_release_error._background_loop_failures == 1,
        "manager release failure was not recorded",
    )
    _assert(
        "release failure worker release failed" in manager_for_release_error._last_background_loop_error,
        f"manager release failure context changed: {manager_for_release_error._last_background_loop_error!r}",
    )
    _assert(
        manager_release_error_recycle_calls == [(0, manager_release_error_worker, "worker release failed")],
        f"manager release failure did not schedule recycle: {manager_release_error_recycle_calls}",
    )

    reward_finally_release_error_pool = ExcelWorkerPool(size=1, platform="windows")
    reward_finally_release_error_worker = _SuccessfulPoolWorker(msg="done", excel_pid=None)
    reward_finally_release_error_pool._workers = [reward_finally_release_error_worker]
    reward_finally_release_error_recycles: list[tuple[int, object, str]] = []

    def record_reward_finally_release_error_recycle(idx: int, *, expected, reason: str) -> None:
        reward_finally_release_error_recycles.append((idx, expected, reason))

    reward_finally_release_error_pool._schedule_recycle = record_reward_finally_release_error_recycle
    reward_finally_release_error_pool._worker_recently_verified_healthy = lambda worker: False
    reward_finally_release_error_pool._worker_is_healthy_bounded = failing_release_health
    reward_after_release_error, msg_after_release_error = (
        await reward_finally_release_error_pool.run_job_with_worker(
            reward_finally_release_error_worker,
            job_id="reward-finally-release-error",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=1.0,
        )
    )
    _assert(
        reward_after_release_error == 1.0,
        f"release failure clobbered completed reward: {reward_after_release_error!r}",
    )
    _assert(
        msg_after_release_error == "done",
        f"release failure clobbered completed reward msg: {msg_after_release_error!r}",
    )
    _assert(
        reward_finally_release_error_recycles
        == [(0, reward_finally_release_error_worker, "worker release failed")],
        f"reward release failure did not schedule recycle: {reward_finally_release_error_recycles}",
    )

    recalc_finally_release_error_pool = ExcelWorkerPool(size=1, platform="windows")
    recalc_finally_release_error_worker = _SuccessfulPoolWorker(msg="recalculated", excel_pid=None)
    recalc_finally_release_error_pool._workers = [recalc_finally_release_error_worker]
    recalc_finally_release_error_recycles: list[tuple[int, object, str]] = []

    def record_recalc_finally_release_error_recycle(idx: int, *, expected, reason: str) -> None:
        recalc_finally_release_error_recycles.append((idx, expected, reason))

    recalc_finally_release_error_pool._schedule_recycle = record_recalc_finally_release_error_recycle
    recalc_finally_release_error_pool._worker_recently_verified_healthy = lambda worker: False
    recalc_finally_release_error_pool._worker_is_healthy_bounded = failing_release_health
    recalc_after_release_error, recalc_msg_after_release_error = (
        await recalc_finally_release_error_pool.recalc_file_with_worker(
            recalc_finally_release_error_worker,
            job_id="recalc-finally-release-error",
            proc_file=Path("proc.xlsx"),
            timeout_s=1.0,
        )
    )
    _assert(recalc_after_release_error is True, "release failure clobbered completed recalc result")
    _assert(
        recalc_msg_after_release_error == "recalculated",
        f"release failure clobbered completed recalc msg: {recalc_msg_after_release_error!r}",
    )
    _assert(
        recalc_finally_release_error_recycles
        == [(0, recalc_finally_release_error_worker, "worker release failed")],
        f"recalc release failure did not schedule recycle: {recalc_finally_release_error_recycles}",
    )

    recalc_failure_release_error_pool = ExcelWorkerPool(size=1, platform="windows")
    recalc_failure_release_error_worker = _FailingPoolWorker(msg="bad workbook")
    recalc_failure_release_error_pool._workers = [recalc_failure_release_error_worker]
    recalc_failure_release_error_recycles: list[tuple[int, object, str]] = []

    def record_recalc_failure_release_error_recycle(idx: int, *, expected, reason: str) -> None:
        recalc_failure_release_error_recycles.append((idx, expected, reason))

    recalc_failure_release_error_pool._schedule_recycle = record_recalc_failure_release_error_recycle
    recalc_failure_release_error_pool._worker_recently_verified_healthy = lambda worker: False
    recalc_failure_release_error_pool._worker_is_healthy_bounded = failing_release_health
    failed_recalc_after_release_error, failed_recalc_msg_after_release_error = (
        await recalc_failure_release_error_pool.recalc_file_with_worker(
            recalc_failure_release_error_worker,
            job_id="recalc-failure-finally-release-error",
            proc_file=Path("proc.xlsx"),
            timeout_s=1.0,
        )
    )
    _assert(
        failed_recalc_after_release_error is False,
        "release failure clobbered worker-reported recalc failure",
    )
    _assert(
        failed_recalc_msg_after_release_error == "bad workbook",
        f"release failure clobbered worker-reported recalc msg: {failed_recalc_msg_after_release_error!r}",
    )
    _assert(
        recalc_failure_release_error_recycles
        == [(0, recalc_failure_release_error_worker, "worker release failed")],
        f"worker-reported recalc release failure did not schedule recycle: {recalc_failure_release_error_recycles}",
    )

    original_liveness_creation_time = windows_process_mod._process_creation_time
    original_liveness_powershell_creation_time = windows_process_mod._windows_powershell_creation_time
    try:
        excel_dead_pool = ExcelWorkerPool(size=1, platform="windows")
        excel_dead_worker = ExcelWorkerProcess(platform="windows", loop=asyncio.get_running_loop())
        excel_dead_worker._proc = _FakeStartedProc()
        excel_dead_worker.excel_pid = 4321
        excel_dead_worker.excel_creation_time = 1111
        excel_dead_pool._workers = [excel_dead_worker]
        excel_dead_pool._available.put_nowait(excel_dead_worker)
        excel_dead_recycle_calls: list[tuple[int, object, str]] = []

        def record_excel_dead_recycle(idx: int, *, expected, reason: str) -> None:
            excel_dead_recycle_calls.append((idx, expected, reason))

        windows_process_mod._process_creation_time = lambda pid: 2222 if pid == 4321 else None
        windows_process_mod._windows_powershell_creation_time = lambda pid: None
        excel_dead_pool._schedule_recycle = record_excel_dead_recycle
        try:
            await excel_dead_pool.acquire(timeout_s=1.0)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("worker with dead Excel child was acquired")
        _assert(
            excel_dead_recycle_calls
            == [(0, excel_dead_worker, "Excel process was not running when acquired")],
            "dead Excel child was not scheduled for recycle on acquire",
        )
        excel_dead_status = await excel_dead_pool.status()
        _assert(
            excel_dead_status["alive_instances"] == 0,
            f"dead Excel child was counted healthy: {excel_dead_status}",
        )
    finally:
        windows_process_mod._process_creation_time = original_liveness_creation_time
        windows_process_mod._windows_powershell_creation_time = original_liveness_powershell_creation_time

    parallel_health_pool = ExcelWorkerPool(size=3, platform="windows")
    parallel_health_pool._workers = [
        _DelayedHealthWorker(delay_s=0.1, healthy=True),
        _DelayedHealthWorker(delay_s=0.1, healthy=False),
        _DelayedHealthWorker(delay_s=0.1, fail=True),
    ]
    health_started = time.monotonic()
    parallel_status = await parallel_health_pool.status()
    health_elapsed = time.monotonic() - health_started
    _assert(
        health_elapsed < 0.25,
        f"pool health probes were not parallelized: elapsed={health_elapsed:.3f}",
    )
    _assert(parallel_status["alive_instances"] == 1, f"pool health status changed: {parallel_status}")

    original_health_probe_timeout = excel_pool_mod._HEALTH_PROBE_TIMEOUT_S
    excel_pool_mod._HEALTH_PROBE_TIMEOUT_S = 0.05
    try:
        timeout_health_pool = ExcelWorkerPool(size=2, platform="windows")
        timeout_health_pool._workers = [
            _DelayedHealthWorker(delay_s=1.0, healthy=True),
            _DelayedHealthWorker(delay_s=0.0, healthy=True),
        ]
        timeout_started = time.monotonic()
        timeout_status = await timeout_health_pool.status()
        timeout_elapsed = time.monotonic() - timeout_started
        _assert(
            timeout_elapsed < 0.3,
            f"pool health timeout did not bound status latency: elapsed={timeout_elapsed:.3f}",
        )
        _assert(timeout_status["alive_instances"] == 1, f"timed-out health probe was counted alive: {timeout_status}")
    finally:
        excel_pool_mod._HEALTH_PROBE_TIMEOUT_S = original_health_probe_timeout

    status_cache_pool = ExcelWorkerPool(size=1, platform="windows")
    old_status_worker = _CountingHealthWorker(healthy=True)
    new_status_worker = _CountingHealthWorker(healthy=False)
    status_cache_pool._workers = [old_status_worker]
    old_status = await status_cache_pool.status()
    _assert(old_status["alive_instances"] == 1, f"initial cached status changed: {old_status}")
    cached_status = await status_cache_pool.status()
    _assert(cached_status["alive_instances"] == 1, f"cached status changed: {cached_status}")
    _assert(old_status_worker.health_calls == 1, "status health cache did not reuse first worker result")
    old_status_worker.is_running = False
    dead_cached_status = await status_cache_pool.status()
    _assert(dead_cached_status["alive_instances"] == 0, f"dead worker stayed alive through TTL cache: {dead_cached_status}")
    _assert(old_status_worker.health_calls == 1, "dead worker status re-ran the expensive health probe")
    _assert(not status_cache_pool._status_health_cache, "dead worker status cache entry was not cleared")
    old_status_worker.is_running = True
    status_cache_pool._workers = [new_status_worker]
    status_cache_pool._status_health_cache[id(new_status_worker)] = (time.monotonic(), True)
    new_status = await status_cache_pool.status()
    _assert(new_status["alive_instances"] == 0, f"status cache served aliased old health: {new_status}")
    _assert(new_status_worker.health_calls == 1, "status cache did not probe replacement worker")
    _assert(
        all(key is new_status_worker for key in status_cache_pool._status_health_cache),
        "status cache retained a retired or non-object worker key",
    )

    unhealthy_cache_pool = ExcelWorkerPool(size=1, platform="windows")
    unhealthy_cache_worker = _CountingHealthWorker(healthy=True)
    unhealthy_cache_pool._workers = [unhealthy_cache_worker]
    unhealthy_cache_pool._status_health_cache[unhealthy_cache_worker] = (time.monotonic(), False)
    unhealthy_cached_status = await unhealthy_cache_pool.status()
    _assert(
        unhealthy_cached_status["alive_instances"] == 0,
        f"cached unhealthy status was not served: {unhealthy_cached_status}",
    )
    _assert(unhealthy_cache_worker.health_calls == 0, "cached unhealthy status re-ran health probe")

    release_cache_pool = ExcelWorkerPool(size=1, platform="windows")
    release_cache_worker = _CountingHealthWorker(healthy=True)
    release_cache_pool._workers = [release_cache_worker]
    release_cache_pool._record_worker_healthy_now(release_cache_worker)
    await release_cache_pool.release(release_cache_worker)
    _assert(release_cache_worker.health_calls == 0, "release ignored recent healthy cache")
    _assert(release_cache_pool._available.qsize() == 1, "cached release did not requeue worker")
    _assert(
        release_cache_pool._available.get_nowait() is release_cache_worker,
        "cached release requeued the wrong worker",
    )

    expired_release_pool = ExcelWorkerPool(size=1, platform="windows")
    expired_release_worker = _CountingHealthWorker(healthy=True)
    expired_release_pool._workers = [expired_release_worker]
    expired_release_pool._status_health_cache[expired_release_worker] = (
        time.monotonic() - excel_pool_mod._HEALTH_STATUS_TTL_S - 0.1,
        True,
    )
    await expired_release_pool.release(expired_release_worker)
    _assert(expired_release_worker.health_calls == 1, "release did not probe after health cache TTL")
    _assert(expired_release_pool._available.qsize() == 1, "expired-cache release did not requeue worker")

    replace_cache_pool = ExcelWorkerPool(size=1, platform="windows")
    replace_cache_worker = _ReplacementPoolWorker()
    replace_cache_pool._workers = [replace_cache_worker]
    replace_cache_pool._record_worker_healthy_now(replace_cache_worker)
    replacement_cache_workers: list[_ReplacementPoolWorker] = []

    class _CacheReplacementPoolWorker(_ReplacementPoolWorker):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            replacement_cache_workers.append(self)

    original_excel_worker_process_cls = excel_pool_mod.ExcelWorkerProcess
    excel_pool_mod.ExcelWorkerProcess = _CacheReplacementPoolWorker
    try:
        replacement_result = await replace_cache_pool._replace_failed_worker(replace_cache_worker)
        _assert(replacement_result is None, "failed worker replacement returned a foreground worker")
        _assert(len(replace_cache_pool._restart_tasks) == 1, "failed worker replacement was not scheduled")
        restart_tasks = list(replace_cache_pool._restart_tasks.values())
        await asyncio.gather(*restart_tasks)
    finally:
        await asyncio.gather(*list(replace_cache_pool._restart_tasks.values()), return_exceptions=True)
        excel_pool_mod.ExcelWorkerProcess = original_excel_worker_process_cls
    replacement_worker = replacement_cache_workers[0]
    _assert(replace_cache_pool._workers == [replacement_worker], "background replacement did not swap pool worker")
    _assert(replace_cache_worker not in replace_cache_pool._status_health_cache, "replacement kept old cache entry")
    _assert(
        replacement_worker not in replace_cache_pool._status_health_cache,
        "replacement worker started with stale cache entry",
    )

    original_pool_worker_start = ExcelWorkerProcess.start
    original_pool_worker_shutdown = ExcelWorkerProcess.shutdown
    parallel_start_calls: list[ExcelWorkerProcess] = []
    parallel_shutdown_calls: list[ExcelWorkerProcess] = []

    async def delayed_pool_worker_start(self) -> None:
        parallel_start_calls.append(self)
        await asyncio.sleep(0.05)

    async def parallel_recording_shutdown(self, *, force: bool = False) -> None:
        parallel_shutdown_calls.append(self)
        await asyncio.sleep(0.05)

    ExcelWorkerProcess.start = delayed_pool_worker_start
    ExcelWorkerProcess.shutdown = parallel_recording_shutdown
    try:
        parallel_start_pool = ExcelWorkerPool(size=3, platform="windows")
        startup_started = time.monotonic()
        await parallel_start_pool.start()
        startup_elapsed = time.monotonic() - startup_started
        _assert(
            startup_elapsed < 0.12,
            f"pool workers were not started concurrently: elapsed={startup_elapsed:.3f}",
        )
        _assert(len(parallel_start_calls) == 3, f"pool did not start all workers: {parallel_start_calls}")
        _assert(parallel_start_pool._available.qsize() == 3, "successful startup did not populate availability")
        shutdown_started = time.monotonic()
        await parallel_start_pool.shutdown(force=True)
        shutdown_elapsed = time.monotonic() - shutdown_started
        _assert(
            shutdown_elapsed < 0.12,
            f"pool workers were not shut down concurrently: elapsed={shutdown_elapsed:.3f}",
        )
        _assert(len(parallel_shutdown_calls) == 3, "parallel startup test did not shut down all workers")
    finally:
        ExcelWorkerProcess.start = original_pool_worker_start
        ExcelWorkerProcess.shutdown = original_pool_worker_shutdown

    original_pool_worker_start = ExcelWorkerProcess.start
    original_pool_worker_shutdown = ExcelWorkerProcess.shutdown
    degraded_start_calls: list[ExcelWorkerProcess] = []
    degraded_shutdown_calls: list[tuple[ExcelWorkerProcess, bool]] = []
    slow_initial_cancelled = asyncio.Event()

    async def degraded_pool_worker_start(self) -> None:
        degraded_start_calls.append(self)
        call_number = len(degraded_start_calls)
        if call_number == 1:
            await asyncio.sleep(0.01)
            return
        if call_number == 2:
            raise RuntimeError("startup boom")
        if call_number == 3:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                slow_initial_cancelled.set()
                raise
        raise AssertionError("unexpected degraded-start replacement")

    async def degraded_recording_shutdown(self, *, force: bool = False) -> None:
        degraded_shutdown_calls.append((self, force))

    ExcelWorkerProcess.start = degraded_pool_worker_start
    ExcelWorkerProcess.shutdown = degraded_recording_shutdown
    degraded_start_pool = ExcelWorkerPool(size=3, platform="windows")
    try:
        degraded_start_started = time.monotonic()
        await degraded_start_pool.start()
        degraded_start_elapsed = time.monotonic() - degraded_start_started
        initial_workers = degraded_start_calls[:3]
        _assert(
            degraded_start_elapsed < 0.12,
            f"partial failure waited for a slow sibling startup: elapsed={degraded_start_elapsed:.3f}",
        )
        _assert(slow_initial_cancelled.is_set(), "slow initial startup was not cancelled")
        _assert(degraded_start_pool._workers == initial_workers, "degraded startup changed slot ordering")
        _assert(degraded_start_pool._available.qsize() == 1, "degraded startup did not expose healthy worker")
        _assert(
            len(degraded_shutdown_calls) == 2
            and {worker for worker, _ in degraded_shutdown_calls} == {initial_workers[1], initial_workers[2]}
            and all(force for _, force in degraded_shutdown_calls),
            f"degraded startup did not clean failed and unfinished candidates: {degraded_shutdown_calls}",
        )
        _assert(len(degraded_start_pool._restart_tasks) == 2, "degraded startup did not restart missing slots")
    finally:
        await degraded_start_pool.shutdown(force=True)
        ExcelWorkerProcess.start = original_pool_worker_start
        ExcelWorkerProcess.shutdown = original_pool_worker_shutdown

    original_pool_worker_start = ExcelWorkerProcess.start
    original_pool_worker_shutdown = ExcelWorkerProcess.shutdown
    partial_start_calls: list[ExcelWorkerProcess] = []
    partial_shutdown_calls: list[tuple[ExcelWorkerProcess, bool]] = []
    partial_replacement_started = asyncio.Event()
    partial_replacement_release = asyncio.Event()

    async def one_failing_pool_worker_start(self) -> None:
        partial_start_calls.append(self)
        call_number = len(partial_start_calls)
        if call_number == 2:
            raise RuntimeError("startup boom")
        if call_number <= 3:
            await asyncio.sleep(0.01)
            return
        partial_replacement_started.set()
        await partial_replacement_release.wait()

    async def partial_recording_shutdown(self, *, force: bool = False) -> None:
        partial_shutdown_calls.append((self, force))

    ExcelWorkerProcess.start = one_failing_pool_worker_start
    ExcelWorkerProcess.shutdown = partial_recording_shutdown
    partial_start_pool = ExcelWorkerPool(size=3, platform="windows")
    try:
        await partial_start_pool.start()
        initial_workers = partial_start_calls[:3]
        failed_initial_worker = initial_workers[1]
        _assert(partial_start_pool._workers == initial_workers, "partial startup changed slot ordering")
        _assert(partial_start_pool._available.qsize() == 2, "partial startup did not expose healthy workers")
        _assert(
            partial_shutdown_calls == [(failed_initial_worker, True)],
            f"partial startup did not clean only failed candidate: {partial_shutdown_calls}",
        )
        _assert(len(partial_start_pool._restart_tasks) == 1, "partial startup did not schedule failed slot restart")
        restart_tasks = list(partial_start_pool._restart_tasks.values())
        await asyncio.wait_for(partial_replacement_started.wait(), timeout=1.0)
        replacement_worker = partial_start_calls[3]
        _assert(
            partial_start_pool._workers[1] is failed_initial_worker,
            "partial startup swapped failed slot before replacement was ready",
        )
        partial_replacement_release.set()
        await asyncio.gather(*restart_tasks)
        _assert(
            partial_start_pool._workers
            == [initial_workers[0], replacement_worker, initial_workers[2]],
            "partial startup background restart did not preserve slot ordering",
        )
        _assert(partial_start_pool._available.qsize() == 3, "partial startup did not restore full capacity")
        await partial_start_pool.shutdown(force=True)
    finally:
        partial_replacement_release.set()
        await asyncio.gather(*list(partial_start_pool._restart_tasks.values()), return_exceptions=True)
        await partial_start_pool.shutdown(force=True)
        ExcelWorkerProcess.start = original_pool_worker_start
        ExcelWorkerProcess.shutdown = original_pool_worker_shutdown

    original_pool_worker_start = ExcelWorkerProcess.start
    original_pool_worker_shutdown = ExcelWorkerProcess.shutdown
    failed_start_calls: list[ExcelWorkerProcess] = []
    failed_shutdown_calls: list[tuple[ExcelWorkerProcess, bool]] = []
    failed_start_cleanup_entered = asyncio.Event()

    async def all_failing_pool_worker_start(self) -> None:
        failed_start_calls.append(self)
        raise RuntimeError("all startup failed")

    async def delayed_failed_start_shutdown(self, *, force: bool = False) -> None:
        failed_shutdown_calls.append((self, force))
        if len(failed_shutdown_calls) == 3:
            failed_start_cleanup_entered.set()
        await asyncio.sleep(0.05)

    ExcelWorkerProcess.start = all_failing_pool_worker_start
    ExcelWorkerProcess.shutdown = delayed_failed_start_shutdown
    try:
        failed_start_pool = ExcelWorkerPool(size=3, platform="windows")
        failed_start_cleanup_started = time.monotonic()
        try:
            await failed_start_pool.start()
        except RuntimeError as exc:
            _assert("all startup failed" in str(exc), f"pool startup raised wrong error: {exc}")
        else:
            raise AssertionError("all-failed pool startup did not propagate")
        failed_start_cleanup_elapsed = time.monotonic() - failed_start_cleanup_started
        _assert(
            failed_start_cleanup_elapsed < 0.12,
            f"failed startup candidates were not shut down concurrently: elapsed={failed_start_cleanup_elapsed:.3f}",
        )
        _assert(failed_start_pool._workers == [], "failed pool startup retained workers")
        _assert(failed_start_pool._available.qsize() == 0, "failed pool startup retained available workers")
        _assert(
            len(failed_shutdown_calls) == 3 and all(force for _, force in failed_shutdown_calls),
            f"failed pool startup did not force-shutdown every candidate: {failed_shutdown_calls}",
        )

        failed_start_calls.clear()
        failed_shutdown_calls.clear()
        failed_start_cleanup_entered.clear()
        cancelled_failed_start_pool = ExcelWorkerPool(size=3, platform="windows")
        cancelled_failed_start_task = asyncio.create_task(cancelled_failed_start_pool.start())
        await asyncio.wait_for(failed_start_cleanup_entered.wait(), timeout=1.0)
        cancelled_failed_start_task.cancel()
        try:
            await cancelled_failed_start_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled all-failed pool startup did not propagate cancellation")
        _assert(
            cancelled_failed_start_task.cancelled(),
            "cancelled all-failed pool startup retained only a pending cancellation count",
        )
        _assert(
            len(failed_shutdown_calls) == 3 and all(force for _, force in failed_shutdown_calls),
            f"cancelled all-failed startup did not clean every candidate: {failed_shutdown_calls}",
        )
        _assert(
            cancelled_failed_start_pool._workers == [],
            "cancelled all-failed pool startup retained workers",
        )
        _assert(
            cancelled_failed_start_pool._available.qsize() == 0,
            "cancelled all-failed pool startup retained available workers",
        )
    finally:
        ExcelWorkerProcess.start = original_pool_worker_start
        ExcelWorkerProcess.shutdown = original_pool_worker_shutdown

    original_pool_worker_start = ExcelWorkerProcess.start
    original_pool_worker_shutdown = ExcelWorkerProcess.shutdown
    pool_start_entered = asyncio.Event()
    pool_start_shutdown_calls: list[bool] = []

    async def blocking_pool_worker_start(self) -> None:
        pool_start_entered.set()
        await asyncio.Event().wait()

    async def recording_pool_worker_shutdown(self, *, force: bool = False) -> None:
        pool_start_shutdown_calls.append(force)

    ExcelWorkerProcess.start = blocking_pool_worker_start
    ExcelWorkerProcess.shutdown = recording_pool_worker_shutdown
    try:
        startup_cancel_pool = ExcelWorkerPool(size=1, platform="windows")
        startup_cancel_task = asyncio.create_task(startup_cancel_pool.start())
        await asyncio.wait_for(pool_start_entered.wait(), timeout=1.0)
        startup_cancel_task.cancel()
        try:
            await startup_cancel_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled pool startup did not raise CancelledError")
        _assert(
            pool_start_shutdown_calls == [True],
            f"cancelled pool startup did not force-shutdown in-progress worker: {pool_start_shutdown_calls}",
        )
        _assert(startup_cancel_pool._workers == [], "cancelled pool startup retained workers")
        _assert(startup_cancel_pool._available.qsize() == 0, "cancelled pool startup retained available workers")
    finally:
        ExcelWorkerProcess.start = original_pool_worker_start
        ExcelWorkerProcess.shutdown = original_pool_worker_shutdown

    shutdown_cancel_pool = ExcelWorkerPool(size=2, platform="windows")
    blocking_shutdown_worker = _BlockingShutdownPoolWorker(block=True)
    next_shutdown_worker = _BlockingShutdownPoolWorker(block=True)
    shutdown_cancel_pool._workers = [blocking_shutdown_worker, next_shutdown_worker]
    shutdown_cancel_task = asyncio.create_task(shutdown_cancel_pool.shutdown(force=False))
    await asyncio.wait_for(
        asyncio.gather(
            blocking_shutdown_worker.shutdown_started.wait(),
            next_shutdown_worker.shutdown_started.wait(),
        ),
        timeout=1.0,
    )
    shutdown_cancel_task.cancel()
    try:
        await shutdown_cancel_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled pool shutdown did not raise CancelledError")
    _assert(
        blocking_shutdown_worker.shutdown_calls == [False],
        f"cancelled pool shutdown did not start first worker shutdown: {blocking_shutdown_worker.shutdown_calls}",
    )
    _assert(
        next_shutdown_worker.shutdown_calls == [False],
        f"cancelled pool shutdown did not start second worker concurrently: {next_shutdown_worker.shutdown_calls}",
    )
    _assert(
        blocking_shutdown_worker.shutdown_cancelled and next_shutdown_worker.shutdown_cancelled,
        "cancelled pool shutdown did not cancel every in-flight worker shutdown",
    )
    _assert(shutdown_cancel_pool._workers == [], "cancelled pool shutdown retained worker references")
    _assert(shutdown_cancel_pool._available.qsize() == 0, "cancelled pool shutdown retained available workers")

    repeated_cancel_pool = ExcelWorkerPool(size=2, platform="windows")
    repeated_cancel_workers = tuple(_RepeatedCancelShutdownPoolWorker() for _ in range(2))
    repeated_cancel_pool._workers = list(repeated_cancel_workers)
    repeated_cancel_task = asyncio.create_task(repeated_cancel_pool.shutdown(force=False))
    await asyncio.wait_for(
        asyncio.gather(*(worker.shutdown_started.wait() for worker in repeated_cancel_workers)),
        timeout=1.0,
    )
    repeated_cancel_task.cancel()
    await asyncio.wait_for(
        asyncio.gather(*(worker.cleanup_started.wait() for worker in repeated_cancel_workers)),
        timeout=1.0,
    )
    repeated_cancel_task.cancel()
    await asyncio.sleep(0)
    _assert(not repeated_cancel_task.done(), "repeated cancellation interrupted worker cleanup")
    _assert(
        repeated_cancel_pool._workers == list(repeated_cancel_workers),
        "pool cleared worker references before repeated-cancel cleanup completed",
    )
    for worker in repeated_cancel_workers:
        worker.cleanup_release.set()
    try:
        await repeated_cancel_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("repeatedly cancelled pool shutdown did not raise CancelledError")
    _assert(
        all(worker.cleanup_completed for worker in repeated_cancel_workers),
        "repeatedly cancelled pool shutdown did not complete every worker cleanup",
    )
    _assert(
        not any(worker.cleanup_interrupted for worker in repeated_cancel_workers),
        "repeated cancellation reached in-progress worker cleanup",
    )
    _assert(
        not any(worker.is_running for worker in repeated_cancel_workers),
        "repeatedly cancelled pool shutdown left a worker running",
    )
    _assert(repeated_cancel_pool._workers == [], "repeatedly cancelled pool shutdown retained worker references")
    _assert(
        repeated_cancel_pool._available.qsize() == 0,
        "repeatedly cancelled pool shutdown retained available workers",
    )

    restart_cleanup_pool = ExcelWorkerPool(size=1, platform="windows")
    restart_expected_worker = _BlockingShutdownPoolWorker(block=False)
    restart_cleanup_pool._workers = [restart_expected_worker]
    restart_cleanup_workers: list[object] = []

    class _RestartCleanupWorker:
        jobs_run = 0
        excel_pid = None

        def __init__(self, *args, **kwargs) -> None:
            self.is_running = True
            self.start_started = asyncio.Event()
            self.cleanup_started = asyncio.Event()
            self.cleanup_release = asyncio.Event()
            self.cleanup_completed = False
            self.cleanup_interrupted = False
            self.shutdown_calls: list[bool] = []
            restart_cleanup_workers.append(self)

        async def start(self) -> None:
            self.start_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.shutdown(force=True)
                raise

        async def shutdown(self, *, force: bool = False) -> None:
            self.shutdown_calls.append(force)
            if len(self.shutdown_calls) > 1:
                self.is_running = False
                return
            self.cleanup_started.set()
            try:
                await self.cleanup_release.wait()
            except asyncio.CancelledError:
                self.cleanup_interrupted = True
                raise
            self.cleanup_completed = True
            self.is_running = False

    original_excel_worker_process_cls = excel_pool_mod.ExcelWorkerProcess
    excel_pool_mod.ExcelWorkerProcess = _RestartCleanupWorker
    restart_cleanup_task = None
    try:
        restart_cleanup_pool._schedule_restart(0, expected=restart_expected_worker)
        while not restart_cleanup_workers:
            await asyncio.sleep(0)
        restart_cleanup_worker = restart_cleanup_workers[0]
        await asyncio.wait_for(restart_cleanup_worker.start_started.wait(), timeout=1.0)
        restart_cleanup_task = asyncio.create_task(restart_cleanup_pool.shutdown(force=False))
        await asyncio.wait_for(restart_cleanup_worker.cleanup_started.wait(), timeout=1.0)
        restart_cleanup_task.cancel()
        await asyncio.sleep(0)
        _assert(not restart_cleanup_task.done(), "cancellation interrupted restart cleanup")
        restart_cleanup_task.cancel()
        await asyncio.sleep(0)
        _assert(not restart_cleanup_task.done(), "repeated cancellation interrupted restart cleanup")
        _assert(
            restart_cleanup_pool._workers == [restart_expected_worker],
            "pool cleared slot ownership before restart cleanup completed",
        )
        restart_cleanup_worker.cleanup_release.set()
        try:
            await restart_cleanup_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled restart cleanup did not propagate CancelledError")
    finally:
        for worker in restart_cleanup_workers:
            worker.cleanup_release.set()
        for task in list(restart_cleanup_pool._restart_tasks.values()):
            task.cancel()
        await asyncio.gather(
            *(
                [restart_cleanup_task]
                if restart_cleanup_task is not None
                else []
            ),
            *list(restart_cleanup_pool._restart_tasks.values()),
            return_exceptions=True,
        )
        excel_pool_mod.ExcelWorkerProcess = original_excel_worker_process_cls
    _assert(restart_cleanup_worker.cleanup_completed, "restart replacement cleanup did not complete")
    _assert(not restart_cleanup_worker.cleanup_interrupted, "repeated cancellation reached restart cleanup")
    _assert(not restart_cleanup_worker.is_running, "restart cleanup left replacement running")
    _assert(
        restart_expected_worker.shutdown_calls == [True],
        f"cancelled restart cleanup did not force-shutdown owned worker: {restart_expected_worker.shutdown_calls}",
    )
    _assert(restart_cleanup_pool._restart_tasks == {}, "cancelled restart cleanup retained restart task ownership")
    _assert(restart_cleanup_pool._workers == [], "cancelled restart cleanup retained worker references")

    idempotent_shutdown_pool = ExcelWorkerPool(size=1, platform="windows")
    idempotent_shutdown_worker = _BlockingShutdownPoolWorker(block=False)
    idempotent_shutdown_pool._workers = [idempotent_shutdown_worker]
    idempotent_shutdown_pool._available.put_nowait(idempotent_shutdown_worker)
    await idempotent_shutdown_pool.shutdown(force=False)
    await idempotent_shutdown_pool.shutdown(force=False)
    _assert(
        idempotent_shutdown_worker.shutdown_calls == [False],
        f"pool shutdown was not idempotent: {idempotent_shutdown_worker.shutdown_calls}",
    )
    _assert(idempotent_shutdown_pool._workers == [], "pool shutdown retained worker references")
    _assert(idempotent_shutdown_pool._available.qsize() == 0, "pool shutdown retained available workers")

    cancel_pool = ExcelWorkerPool(size=1, platform="windows")
    hanging_worker = _HangingPoolWorker()
    cancel_pool._workers = [hanging_worker]
    cancel_recycle_calls: list[tuple[int, object, str]] = []
    cancel_restart_calls: list[tuple[int, object]] = []

    def record_cancel_recycle(idx: int, *, expected, reason: str) -> None:
        cancel_recycle_calls.append((idx, expected, reason))

    def record_cancel_restart(idx: int, *, expected) -> None:
        cancel_restart_calls.append((idx, expected))

    cancel_pool._schedule_recycle = record_cancel_recycle
    cancel_pool._schedule_restart = record_cancel_restart
    cancelled_task = asyncio.create_task(
        cancel_pool.run_job_with_worker(
            hanging_worker,
            job_id="cancelled-reward",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=60.0,
        )
    )
    await asyncio.sleep(0)
    cancelled_task.cancel()
    try:
        await cancelled_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled pooled reward task did not raise CancelledError")
    _assert(cancel_pool._available.qsize() == 0, "cancelled pooled reward worker was requeued")
    _assert(hanging_worker.is_running is False, "cancelled pooled reward worker was not quiesced")
    _assert(cancel_recycle_calls == [], f"cancelled reward worker used async recycle: {cancel_recycle_calls}")
    _assert(
        cancel_restart_calls == [(0, hanging_worker)],
        "cancelled reward worker was not scheduled for restart",
    )

    memory_check_pool = ExcelWorkerPool(size=1, platform="windows")
    memory_check_worker = _SuccessfulPoolWorker()
    memory_check_pool._workers = [memory_check_worker]
    memory_check_pool._recycle_private_mb = 1
    memory_check_recycle_calls: list[tuple[int, object, str]] = []
    memory_check_restart_calls: list[tuple[int, object]] = []

    def record_memory_check_recycle(idx: int, *, expected, reason: str) -> None:
        memory_check_recycle_calls.append((idx, expected, reason))

    def record_memory_check_restart(idx: int, *, expected) -> None:
        memory_check_restart_calls.append((idx, expected))

    memory_check_pool._schedule_recycle = record_memory_check_recycle
    memory_check_pool._schedule_restart = record_memory_check_restart
    memory_check_started = threading.Event()
    memory_check_release = threading.Event()
    original_os_name = excel_pool_mod.os.name
    original_process_private_bytes = excel_pool_mod._process_private_bytes

    def blocking_process_private_bytes(pid: int) -> int | None:
        memory_check_started.set()
        memory_check_release.wait(timeout=5.0)
        return 0

    excel_pool_mod.os.name = "nt"
    excel_pool_mod._process_private_bytes = blocking_process_private_bytes
    try:
        memory_check_task = asyncio.create_task(
            memory_check_pool.run_job_with_worker(
                memory_check_worker,
                job_id="cancelled-memory-check",
                gt_file=Path("gt.xlsx"),
                proc_file=Path("proc.xlsx"),
                answer_position="Sheet1!A1",
                timeout_s=60.0,
            )
        )
        started = await asyncio.to_thread(memory_check_started.wait, 5.0)
        _assert(started, "pooled reward memory check did not start")
        memory_check_task.cancel()
        try:
            await memory_check_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled reward memory-check task did not raise CancelledError")
    finally:
        memory_check_release.set()
        excel_pool_mod.os.name = original_os_name
        excel_pool_mod._process_private_bytes = original_process_private_bytes

    _assert(
        memory_check_pool._available.qsize() == 0,
        "cancelled reward worker was requeued after memory-check cancellation",
    )
    _assert(memory_check_worker.is_running is False, "cancelled memory-check worker was not quiesced")
    _assert(
        memory_check_recycle_calls == [],
        f"cancelled memory-check worker used async recycle: {memory_check_recycle_calls}",
    )
    _assert(
        memory_check_restart_calls == [(0, memory_check_worker)],
        "cancelled memory-check worker was not scheduled for restart",
    )

    cancel_recalc_pool = ExcelWorkerPool(size=1, platform="windows")
    hanging_recalc_worker = _HangingPoolWorker()
    cancel_recalc_pool._workers = [hanging_recalc_worker]
    cancel_recalc_calls: list[tuple[int, object, str]] = []
    cancel_recalc_restart_calls: list[tuple[int, object]] = []

    def record_cancel_recalc(idx: int, *, expected, reason: str) -> None:
        cancel_recalc_calls.append((idx, expected, reason))

    def record_cancel_recalc_restart(idx: int, *, expected) -> None:
        cancel_recalc_restart_calls.append((idx, expected))

    cancel_recalc_pool._schedule_recycle = record_cancel_recalc
    cancel_recalc_pool._schedule_restart = record_cancel_recalc_restart
    cancelled_recalc_task = asyncio.create_task(
        cancel_recalc_pool.recalc_file_with_worker(
            hanging_recalc_worker,
            job_id="cancelled-recalc",
            proc_file=Path("proc.xlsx"),
            timeout_s=60.0,
        )
    )
    await asyncio.sleep(0)
    cancelled_recalc_task.cancel()
    try:
        await cancelled_recalc_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled pooled recalc task did not raise CancelledError")
    _assert(cancel_recalc_pool._available.qsize() == 0, "cancelled pooled recalc worker was requeued")
    _assert(hanging_recalc_worker.is_running is False, "cancelled pooled recalc worker was not quiesced")
    _assert(cancel_recalc_calls == [], f"cancelled recalc worker used async recycle: {cancel_recalc_calls}")
    _assert(
        cancel_recalc_restart_calls == [(0, hanging_recalc_worker)],
        "cancelled recalc worker was not scheduled for restart",
    )

    replace_cancel_pool = ExcelWorkerPool(size=1, platform="windows")
    replace_cancel_worker = _ReplacementShutdownWorker()
    replace_cancel_pool._workers = [replace_cancel_worker]
    replace_restart_calls: list[tuple[int, object]] = []

    def record_replace_restart(idx: int, *, expected) -> None:
        replace_restart_calls.append((idx, expected))

    replace_cancel_pool._schedule_restart = record_replace_restart
    replace_task = asyncio.create_task(
        replace_cancel_pool.run_job_with_worker(
            replace_cancel_worker,
            job_id="cancelled-reward-replacement",
            gt_file=Path("gt.xlsx"),
            proc_file=Path("proc.xlsx"),
            answer_position="Sheet1!A1",
            timeout_s=60.0,
        )
    )
    await replace_cancel_worker.shutdown_started.wait()
    replace_task.cancel()
    replace_cancel_worker.shutdown_release.set()
    try:
        await replace_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled reward replacement task did not raise CancelledError")
    _assert(
        replace_restart_calls == [(0, replace_cancel_worker)],
        f"cancelled reward replacement did not schedule a slot restart: {replace_restart_calls}",
    )

    recalc_replace_cancel_pool = ExcelWorkerPool(size=1, platform="windows")
    recalc_replace_cancel_worker = _ReplacementShutdownWorker()
    recalc_replace_cancel_pool._workers = [recalc_replace_cancel_worker]
    recalc_replace_restart_calls: list[tuple[int, object]] = []

    def record_recalc_replace_restart(idx: int, *, expected) -> None:
        recalc_replace_restart_calls.append((idx, expected))

    recalc_replace_cancel_pool._schedule_restart = record_recalc_replace_restart
    recalc_replace_task = asyncio.create_task(
        recalc_replace_cancel_pool.recalc_file_with_worker(
            recalc_replace_cancel_worker,
            job_id="cancelled-recalc-replacement",
            proc_file=Path("proc.xlsx"),
            timeout_s=60.0,
        )
    )
    await recalc_replace_cancel_worker.shutdown_started.wait()
    recalc_replace_task.cancel()
    recalc_replace_cancel_worker.shutdown_release.set()
    try:
        await recalc_replace_task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled recalc replacement task did not raise CancelledError")
    _assert(
        recalc_replace_restart_calls == [(0, recalc_replace_cancel_worker)],
        f"cancelled recalc replacement did not schedule a slot restart: {recalc_replace_restart_calls}",
    )

    with temporary_directory(prefix="async_reward_api_worker_error_capture_") as tmp:
        tmp_path = Path(tmp)
        output_root = tmp_path / "output_root"
        sample_dir = output_root / "thread_1"
        job_dir = sample_dir / ".async_reward_jobs"
        job_dir.mkdir(parents=True)
        target_file = sample_dir / "target.xlsx"
        target_file.write_bytes(b"target")
        proc_file = job_dir / "output_long-error.xlsx"
        original_output_root = os.environ.get("REWARD_API_OUTPUT_ROOT")
        os.environ["REWARD_API_OUTPUT_ROOT"] = str(output_root)
        try:
            capture_store = _CaptureStore()
            manager = manager_mod.RewardJobManager(store=capture_store, platform=Platform.WINDOWS)

            async def failing_compute_reward(*args, **kwargs):
                raise RuntimeError("x" * 600)

            manager._compute_reward = failing_compute_reward
            await manager._run_job(
                JobRecord(
                    job_id="long-error",
                    thread_dir="thread_1",
                    gt_file=target_file,
                    proc_file=proc_file,
                    answer_position="Sheet1!A1",
                    status=JobStatus.RUNNING,
                ),
                excel_worker=None,
                use_excel_pool=False,
            )
            _assert(len(capture_store.finish_calls) == 1, "manager did not store failed job")
            stored_msg = str(capture_store.finish_calls[0]["msg"])
            _assert(len(stored_msg) <= 500, f"stored worker exception was not capped: {len(stored_msg)}")
            _assert(stored_msg.startswith("worker exception:"), f"stored error lost context: {stored_msg!r}")
            _assert(
                capture_store.finish_calls[0]["worker_id"] == manager._worker_id,
                "manager terminal finish did not include worker ownership guard",
            )
        finally:
            if original_output_root is None:
                os.environ.pop("REWARD_API_OUTPUT_ROOT", None)
            else:
                os.environ["REWARD_API_OUTPUT_ROOT"] = original_output_root

    print("OK: worker response handling looks good")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
