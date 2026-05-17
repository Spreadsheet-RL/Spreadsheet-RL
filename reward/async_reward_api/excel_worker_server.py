from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .eval import compute_reward
from .platform import Platform, detect_platform, normalize_platform


XL_CALCULATION_AUTOMATIC = -4105
XL_CALCULATION_MANUAL = -4135


def _format_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


class _WindowsExcelSession:
    def __init__(self) -> None:
        self._pythoncom = None
        self._xl_app = None
        self.excel_pid: int | None = None

    def start(self) -> None:
        try:
            import pythoncom  # pywin32
            import win32process  # pywin32
            from win32com.client import DispatchEx  # pywin32
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"pywin32 import failed: {_format_exception(exc)}") from exc

        pythoncom.CoInitialize()
        self._pythoncom = pythoncom

        xl_app = DispatchEx("Excel.Application")
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
            xl_app.AlertBeforeOverwriting = False
        except Exception:
            pass

        for _ in range(40):
            try:
                hwnd = xl_app.Hwnd
            except Exception:
                hwnd = None
            if hwnd:
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                except Exception:
                    pid = None
                if pid:
                    self.excel_pid = int(pid)
                    break
            time.sleep(0.05)

        if self.excel_pid is None:
            try:
                xl_app.Quit()
            except Exception:
                pass
            raise RuntimeError("failed to determine Excel PID")

        self._xl_app = xl_app

    def shutdown(self) -> None:
        if self._xl_app is not None:
            try:
                self._xl_app.Quit()
            except Exception:
                pass
            self._xl_app = None
        if self._pythoncom is not None:
            try:
                self._pythoncom.CoUninitialize()
            except Exception:
                pass
            self._pythoncom = None

    def recalc_and_save(self, proc_file: Path) -> None:
        if self._xl_app is None:
            raise RuntimeError("Excel session is not started")

        filename = os.path.abspath(str(proc_file))
        xl_book = None
        saved = False
        previous_calculation = None
        save_calculation = XL_CALCULATION_AUTOMATIC
        try:
            try:
                previous_calculation = self._xl_app.Calculation
            except Exception:
                previous_calculation = None
            save_calculation = (
                previous_calculation
                if previous_calculation is not None and previous_calculation != XL_CALCULATION_MANUAL
                else XL_CALCULATION_AUTOMATIC
            )
            try:
                self._xl_app.Calculation = XL_CALCULATION_MANUAL
            except Exception:
                pass
            open_attempts = [
                dict(
                    Filename=filename,
                    UpdateLinks=0,
                    ReadOnly=False,
                    IgnoreReadOnlyRecommended=True,
                    AddToMru=False,
                    Notify=False,
                    CorruptLoad=1,
                ),
                dict(
                    Filename=filename,
                    UpdateLinks=0,
                    ReadOnly=False,
                    IgnoreReadOnlyRecommended=True,
                    AddToMru=False,
                ),
                dict(Filename=filename, UpdateLinks=False, ReadOnly=False),
            ]
            last_exc: Exception | None = None
            for kwargs in open_attempts:
                try:
                    xl_book = self._xl_app.Workbooks.Open(**kwargs)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    continue
            if xl_book is None:
                raise RuntimeError(f"Excel failed to open workbook: {last_exc}") from last_exc

            try:
                self._xl_app.Calculation = save_calculation
            except Exception:
                try:
                    self._xl_app.Calculation = XL_CALCULATION_AUTOMATIC
                except Exception:
                    pass

            calculated_ok = False
            try:
                self._xl_app.CalculateFullRebuild()
                calculated_ok = True
            except Exception:
                try:
                    self._xl_app.Calculate()
                except Exception:
                    calculated_ok = False
                else:
                    calculated_ok = True

            if not calculated_ok:
                raise RuntimeError("Excel calculation failed")

            try:
                self._xl_app.CalculateUntilAsyncQueriesDone()
            except Exception:
                pass

            try:
                self._xl_app.Calculation = save_calculation
            except Exception:
                pass

            xl_book.Save()
            saved = True
        finally:
            if xl_book is not None:
                try:
                    xl_book.Close(SaveChanges=bool(saved))
                except Exception:
                    pass
            if self._xl_app is not None:
                try:
                    self._xl_app.Calculation = save_calculation
                except Exception:
                    pass


def _send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent Excel worker (Windows).")
    parser.add_argument("--platform", choices=["windows"], required=False)
    args = parser.parse_args(argv)

    platform = normalize_platform(args.platform) or detect_platform()
    if platform is not Platform.WINDOWS or os.name != "nt":
        print("excel_worker_server is only supported on Windows", file=sys.stderr, flush=True)
        return 2

    session = _WindowsExcelSession()
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[excel_worker_server] start failed: {_format_exception(exc)}", file=sys.stderr, flush=True)
        return 2

    _send(
        {
            "type": "ready",
            "worker_pid": os.getpid(),
            "excel_pid": session.excel_pid,
        }
    )

    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(req, dict):
                continue

            req_type = req.get("type")
            if req_type == "shutdown":
                break
            if req_type not in {"job", "recalc"}:
                continue

            job_id = str(req.get("job_id") or "")
            try:
                proc_file = Path(str(req["proc_file"]))

                session.recalc_and_save(proc_file)
                if req_type == "recalc":
                    _send(
                        {
                            "type": "result",
                            "job_id": job_id,
                            "ok": True,
                            "reward": 0.0,
                            "msg": "",
                        }
                    )
                    continue

                gt_file = Path(str(req["gt_file"]))
                answer_position = str(req["answer_position"])
                reward, msg = compute_reward(gt_file, proc_file, answer_position)
                _send(
                    {
                        "type": "result",
                        "job_id": job_id,
                        "ok": True,
                        "reward": float(reward),
                        "msg": msg or "",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                _send(
                    {
                        "type": "result",
                        "job_id": job_id,
                        "ok": False,
                        "reward": 0.0,
                        "msg": f"worker error: {_format_exception(exc)}",
                    }
                )
    finally:
        session.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
