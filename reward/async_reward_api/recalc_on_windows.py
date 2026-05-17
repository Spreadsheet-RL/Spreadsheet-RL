from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


XL_CALCULATION_AUTOMATIC = -4105
XL_CALCULATION_MANUAL = -4135


def recalc_spreadsheet(file_path: str | Path, excel_pid_file: str | Path | None = None) -> int:
    """Recalculate all formulas in a spreadsheet and save the cached results."""
    path = Path(file_path)
    if not path.exists():
        return 1

    try:
        import pythoncom  # pywin32
        from win32com.client import DispatchEx  # pywin32
    except Exception as exc:  # noqa: BLE001
        print(f"[recalc_on_windows] pywin32 import failed: {exc}", file=sys.stderr, flush=True)
        return 1

    filename = os.path.abspath(str(path))
    xl_app = None
    xl_book = None
    previous_calculation = None
    save_calculation = XL_CALCULATION_AUTOMATIC

    pythoncom.CoInitialize()
    try:
        xl_app = DispatchEx("Excel.Application")
        try:
            previous_calculation = xl_app.Calculation
        except Exception:
            previous_calculation = None
        save_calculation = (
            previous_calculation
            if previous_calculation is not None and previous_calculation != XL_CALCULATION_MANUAL
            else XL_CALCULATION_AUTOMATIC
        )
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
                    from .excel_pool import _process_creation_time  # defer heavy import

                    creation_time = _process_creation_time(int(excel_pid))
                    if creation_time is None:
                        print(
                            "[recalc_on_windows] Excel creation_time unavailable; skipping pid file",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        payload: dict[str, int] = {
                            "pid": int(excel_pid),
                            "creation_time": int(creation_time),
                        }
                        Path(excel_pid_file).write_text(json.dumps(payload), encoding="utf-8")
                else:
                    print(
                        "[recalc_on_windows] failed to resolve Excel PID for pid file",
                        file=sys.stderr,
                        flush=True,
                    )
                    # Best-effort: continue recalc even if timeout attribution metadata is unavailable.
            except Exception as exc:
                print(
                    f"[recalc_on_windows] failed to write Excel PID file: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                # Best-effort: continue recalc even if pid-file write fails.
        xl_app.Visible = False
        xl_app.DisplayAlerts = False
        xl_app.ScreenUpdating = False
        xl_app.EnableEvents = False
        try:
            xl_app.AskToUpdateLinks = False
        except Exception:
            pass
        try:
            xl_app.Interactive = False
        except Exception:
            pass
        try:
            xl_app.UserControl = False
        except Exception:
            pass
        try:
            xl_app.AutomationSecurity = 1
        except Exception:
            pass
        try:
            xl_app.Calculation = XL_CALCULATION_MANUAL
        except Exception:
            pass

        try:
            xl_book = xl_app.Workbooks.Open(
                Filename=filename,
                UpdateLinks=0,
                ReadOnly=False,
                IgnoreReadOnlyRecommended=True,
                AddToMru=False,
            )
        except Exception:
            xl_book = xl_app.Workbooks.Open(Filename=filename, UpdateLinks=False, ReadOnly=False)

        try:
            xl_app.Calculation = save_calculation
        except Exception:
            try:
                xl_app.Calculation = XL_CALCULATION_AUTOMATIC
            except Exception:
                pass

        calculated_ok = False
        try:
            xl_app.CalculateFullRebuild()
            calculated_ok = True
        except Exception:
            try:
                xl_app.Calculate()
            except Exception:
                calculated_ok = False
            else:
                calculated_ok = True

        if not calculated_ok:
            raise RuntimeError("Excel calculation failed")

        try:
            # Not available on all Excel versions / builds; best-effort.
            xl_app.CalculateUntilAsyncQueriesDone()
        except Exception:
            pass

        try:
            xl_app.Calculation = save_calculation
        except Exception:
            pass

        xl_book.Save()
        xl_book.Close(SaveChanges=True)
        xl_book = None
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[recalc_on_windows] Excel recalc failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if xl_book is not None:
            try:
                xl_book.Close(SaveChanges=False)
            except Exception:
                pass
        if xl_app is not None:
            try:
                xl_app.Calculation = save_calculation
            except Exception:
                pass
            try:
                xl_app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()
