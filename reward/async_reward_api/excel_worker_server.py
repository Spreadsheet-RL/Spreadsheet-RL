from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .eval import compute_reward
from .excel_com import FatalExcelSessionError, configure_excel_app, recalc_and_save_workbook
from .messages import format_exception as _format_exception
from .messages import public_worker_message as _public_worker_message
from .platform import Platform, allow_unsupported_host_for_tests, detect_platform, normalize_platform


logger = logging.getLogger(__name__)


class _WindowsExcelSession:
    def __init__(self) -> None:
        self._pythoncom = None
        self._xl_app = None
        self.excel_pid: int | None = None

    def start(self) -> None:
        xl_app = None
        try:
            import pythoncom  # pywin32
            import win32process  # pywin32
            from win32com.client import DispatchEx  # pywin32
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"pywin32 import failed: {_format_exception(exc)}") from exc

        try:
            pythoncom.CoInitialize()
            self._pythoncom = pythoncom

            xl_app = DispatchEx("Excel.Application")
            configure_excel_app(xl_app)

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
        except Exception:
            if self._xl_app is None and xl_app is not None:
                try:
                    xl_app.Quit()
                except Exception:
                    pass
            if self._pythoncom is not None:
                try:
                    self._pythoncom.CoUninitialize()
                except Exception:
                    pass
                self._pythoncom = None
            raise

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
        recalc_and_save_workbook(self._xl_app, os.path.abspath(str(proc_file)))


def _send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent Excel worker (Windows).")
    parser.add_argument("--platform", choices=["windows"], required=False)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    platform = normalize_platform(args.platform) or detect_platform()
    # tests/08_worker_response_handling.py fakes COM on non-Windows hosts.
    if platform is not Platform.WINDOWS or (os.name != "nt" and not allow_unsupported_host_for_tests()):
        logger.warning("excel_worker_server is only supported on Windows")
        return 2

    session = _WindowsExcelSession()
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[excel_worker_server] start failed: {_format_exception(exc)}")
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
                public_msg = _public_worker_message(msg, fallback="")
                _send(
                    {
                        "type": "result",
                        "job_id": job_id,
                        "ok": True,
                        "reward": float(reward),
                        "msg": public_msg,
                    }
                )
            except FatalExcelSessionError as exc:
                raw_msg = f"fatal worker error: {_format_exception(exc)}"
                logger.warning(f"[excel_worker_server] {raw_msg}")
                _send(
                    {
                        "type": "result",
                        "job_id": job_id,
                        "ok": False,
                        "reward": 0.0,
                        "msg": _public_worker_message(raw_msg, fallback="fatal worker error"),
                    }
                )
                return 1
            except Exception as exc:  # noqa: BLE001
                raw_msg = f"worker error: {_format_exception(exc)}"
                logger.warning(f"[excel_worker_server] {raw_msg}")
                _send(
                    {
                        "type": "result",
                        "job_id": job_id,
                        "ok": False,
                        "reward": 0.0,
                        "msg": _public_worker_message(raw_msg, fallback="worker error"),
                    }
                )
    finally:
        session.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
