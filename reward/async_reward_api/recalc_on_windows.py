from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .excel_com import FatalExcelSessionError
from .excel_com import configure_excel_app, recalc_and_save_workbook
from .messages import public_worker_message as _public_worker_message
from .windows_process import _process_creation_time, _windows_powershell_creation_time


logger = logging.getLogger(__name__)

def _write_text_atomic(path: Path, text: str) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def recalc_spreadsheet(file_path: str | Path, excel_pid_file: str | Path | None = None) -> tuple[int, str]:
    """Recalculate all formulas in a spreadsheet and save the cached results."""
    path = Path(file_path)
    if not path.exists():
        return 1, "input workbook does not exist"

    try:
        import pythoncom  # pywin32
        from win32com.client import DispatchEx  # pywin32
    except Exception as exc:  # noqa: BLE001
        msg = f"pywin32 import failed: {exc}"
        logger.warning(f"[recalc_on_windows] {msg}")
        return 1, _public_worker_message(msg, fallback="pywin32 import failed")

    filename = os.path.abspath(str(path))
    xl_app = None
    com_initialized = False

    try:
        pythoncom.CoInitialize()
        com_initialized = True
        xl_app = DispatchEx("Excel.Application")
        if excel_pid_file is not None:
            try:
                from win32process import GetWindowThreadProcessId  # pywin32

                excel_pid = 0
                for _ in range(40):
                    try:
                        hwnd = int(xl_app.Hwnd)
                    except Exception:
                        hwnd = 0
                    if hwnd:
                        try:
                            _, excel_pid = GetWindowThreadProcessId(hwnd)
                        except Exception:
                            excel_pid = 0
                    if excel_pid:
                        break
                    time.sleep(0.1)
                if excel_pid:
                    creation_time = _process_creation_time(int(excel_pid))
                    if creation_time is None:
                        creation_time = _windows_powershell_creation_time(int(excel_pid))
                    if creation_time is None:
                        logger.warning("[recalc_on_windows] Excel creation_time unavailable; skipping pid file")
                    else:
                        payload: dict[str, int] = {
                            "pid": int(excel_pid),
                            "creation_time": int(creation_time),
                        }
                        _write_text_atomic(Path(excel_pid_file), json.dumps(payload))
                else:
                    logger.warning("[recalc_on_windows] failed to resolve Excel PID for pid file")
                    # Best-effort: continue recalc even if timeout attribution metadata is unavailable.
            except Exception as exc:
                logger.warning(f"[recalc_on_windows] failed to write Excel PID file: {exc}")
                # Best-effort: continue recalc even if pid-file write fails.
        configure_excel_app(xl_app)
        recalc_and_save_workbook(xl_app, filename)
        return 0, ""
    except FatalExcelSessionError as exc:
        msg = f"Excel recalc fatal failure: {exc}"
        logger.error(f"[recalc_on_windows] {msg}")
        return 2, _public_worker_message(msg, fallback="Excel recalc fatal failure")
    except Exception as exc:  # noqa: BLE001
        msg = f"Excel recalc failed: {exc}"
        logger.warning(f"[recalc_on_windows] {msg}")
        return 1, _public_worker_message(msg, fallback="Excel recalc failed")
    finally:
        if xl_app is not None:
            try:
                xl_app.Quit()
            except Exception:
                pass
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
