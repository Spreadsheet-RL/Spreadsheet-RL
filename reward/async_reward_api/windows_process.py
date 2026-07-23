from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .platform import Platform


def _creationflags_no_window() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def _worker_creationflags() -> int:
    flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return flags


def _run_kwargs_no_window() -> dict[str, int]:
    flags = _creationflags_no_window()
    return {"creationflags": flags} if flags else {}


def _process_private_bytes(pid: int) -> int | None:
    if os.name != "nt" or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        get_process_memory_info = psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        get_process_memory_info.restype = wintypes.BOOL

        handle = None
        for access in (0x1000, 0x0400):
            handle = open_process(access, False, int(pid))
            if handle:
                break
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            ok = bool(
                get_process_memory_info(
                    handle,
                    ctypes.byref(counters),
                    wintypes.DWORD(ctypes.sizeof(counters)),
                )
            )
            if not ok:
                return None
            return int(counters.PrivateUsage)
        finally:
            close_handle(handle)
    except Exception:
        return None


def _windows_powershell_creation_time(pid: int) -> int | None:
    if os.name != "nt" or pid <= 0:
        return None
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Process -Id {int(pid)} -ErrorAction Stop).StartTime.ToFileTimeUtc()",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_run_kwargs_no_window(),
        )
        text = (completed.stdout or "").strip()
        if not text:
            return None
        return int(text)
    except Exception:
        return None


def _process_creation_time(pid: int) -> int | None:
    if os.name != "nt" or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE

        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        get_process_times.restype = wintypes.BOOL

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = None
        for access in (0x1000, 0x0400):
            handle = open_process(access, False, int(pid))
            if handle:
                break
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = bool(
                get_process_times(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
            )
            if not ok:
                return None
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            close_handle(handle)
    except Exception:
        return None


def _process_creation_time_with_fallback(pid: int) -> int | None:
    current = _process_creation_time(pid)
    if current is None:
        current = _windows_powershell_creation_time(pid)
    return current


def _tasklist_image_name(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_run_kwargs_no_window(),
        )
        text = (completed.stdout or "").strip()
        if not text:
            return None
        rows = [line for line in text.splitlines() if line.strip()]
        if not rows:
            return None
        line = rows[0]
        if line.upper().startswith("INFO:"):
            return None
        row = next(csv.reader([line]))
        if not row:
            return None
        return str(row[0])
    except Exception:
        return None


def _pid_is_excel(*, platform: Platform, pid: int) -> bool:
    if platform is not Platform.WINDOWS or pid <= 0:
        return False
    return (_tasklist_image_name(pid) or "").strip().upper() == "EXCEL.EXE"


def _taskkill_tree(pid: int) -> None:
    if os.name != "nt" or pid <= 0:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            **_run_kwargs_no_window(),
        )
    except Exception:
        return


def _taskkill_process_tree_if_matches(pid: int, expected_creation_time: int | None = None) -> None:
    if pid <= 0 or expected_creation_time is None:
        return
    current = _process_creation_time_with_fallback(pid)
    if current is None or int(current) != int(expected_creation_time):
        return
    _taskkill_tree(pid)


def _kill_excel_pid(
    *,
    platform: Platform,
    pid: int,
    expected_creation_time: int | None = None,
) -> bool:
    if platform is not Platform.WINDOWS or pid <= 0 or expected_creation_time is None:
        return False
    current = _process_creation_time_with_fallback(pid)
    if current is None or int(current) != int(expected_creation_time):
        return False
    if not _pid_is_excel(platform=platform, pid=pid):
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            **_run_kwargs_no_window(),
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


def _taskkill_pid(pid: int, expected_creation_time: int | None = None) -> None:
    _kill_excel_pid(
        platform=Platform.WINDOWS,
        pid=pid,
        expected_creation_time=expected_creation_time,
    )


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _process_creation_time_with_fallback(pid) is not None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_matches_creation_time(pid: int, expected_creation_time: int | None) -> bool:
    if pid <= 0 or expected_creation_time is None:
        return False
    current = _process_creation_time_with_fallback(pid)
    return current is not None and int(current) == int(expected_creation_time)


def _kill_subprocess_tree(proc: subprocess.Popen[bytes]) -> None:
    pid = int(proc.pid or 0)
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                **({"creationflags": _worker_creationflags()} if _worker_creationflags() else {}),
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


def _kill_excel_pid_from_file(*, platform: Platform, pid_file: Path) -> bool:
    pid = 0
    expected_creation_time: int | None = None
    for attempt in range(6):
        try:
            text = pid_file.read_text(encoding="utf-8").strip()
            if not text.startswith("{"):
                raise ValueError("Excel PID file did not contain JSON")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("Excel PID file JSON was not an object")
            pid = int(payload.get("pid") or 0)
            raw_creation_time = payload.get("creation_time")
            if raw_creation_time is None:
                raise ValueError("Excel PID file missed creation_time")
            expected_creation_time = int(raw_creation_time)
            break
        except Exception:
            if attempt == 5:
                return False
            time.sleep(0.05)
    return _kill_excel_pid(
        platform=platform,
        pid=pid,
        expected_creation_time=expected_creation_time,
    )


def _list_excel_pids(platform: Platform) -> set[int] | None:
    if platform is not Platform.WINDOWS:
        return set()
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_run_kwargs_no_window(),
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
    except FileNotFoundError:
        return set()
    except Exception:
        return None


def _kill_new_excel_processes(*, platform: Platform, baseline_pids: set[int] | None) -> int:
    if baseline_pids is None:
        return 0
    current = _list_excel_pids(platform)
    if current is None:
        return 0
    to_kill = [pid for pid in current if pid not in baseline_pids]
    killed = 0
    for pid in to_kill:
        creation_time = _process_creation_time_with_fallback(pid) if platform is Platform.WINDOWS else None
        if _kill_excel_pid(
            platform=platform,
            pid=pid,
            expected_creation_time=creation_time,
        ):
            killed += 1
    return killed
