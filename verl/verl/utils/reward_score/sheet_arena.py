from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Tuple

from verl.utils.paths import get_spreadsheet_rl_data_root, normalize_thread_dir, normalize_workspace_id

_DEFAULT_SPREADSHEET_RL_REWARD_TIMEOUT_S = 10800.0  # 3 hours minus buffer
logger = logging.getLogger(__name__)

_spreadsheet_rl_reward_semaphore: threading.BoundedSemaphore | None = None
_spreadsheet_rl_reward_semaphore_initialized = False
_spreadsheet_rl_reward_semaphore_lock = threading.Lock()


def _get_max_concurrent_reward_requests() -> int | None:
    value = os.environ.get("SPREADSHEET_RL_REWARD_MAX_CONCURRENT")
    if value is None:
        return None
    try:
        max_concurrent = int(value)
    except ValueError:
        return None
    if max_concurrent <= 0:
        return None
    return max_concurrent


def _get_reward_semaphore() -> threading.BoundedSemaphore | None:
    global _spreadsheet_rl_reward_semaphore
    global _spreadsheet_rl_reward_semaphore_initialized
    if _spreadsheet_rl_reward_semaphore_initialized:
        return _spreadsheet_rl_reward_semaphore
    with _spreadsheet_rl_reward_semaphore_lock:
        if _spreadsheet_rl_reward_semaphore_initialized:
            return _spreadsheet_rl_reward_semaphore
        max_concurrent = _get_max_concurrent_reward_requests()
        _spreadsheet_rl_reward_semaphore = threading.BoundedSemaphore(max_concurrent) if max_concurrent else None
        _spreadsheet_rl_reward_semaphore_initialized = True
        return _spreadsheet_rl_reward_semaphore


def _get_reward_timeout_s() -> float:
    value = os.environ.get("SPREADSHEET_RL_REWARD_TIMEOUT_S")
    if value is None:
        return _DEFAULT_SPREADSHEET_RL_REWARD_TIMEOUT_S
    try:
        timeout_s = float(value)
        if timeout_s <= 0:
            return _DEFAULT_SPREADSHEET_RL_REWARD_TIMEOUT_S
        return timeout_s
    except ValueError:
        return _DEFAULT_SPREADSHEET_RL_REWARD_TIMEOUT_S


def _get_poll_wait_s() -> float:
    value = os.environ.get("SPREADSHEET_RL_REWARD_POLL_WAIT_S", "10")
    try:
        poll_wait_s = float(value)
        return min(max(0.0, poll_wait_s), 25.0)
    except ValueError:
        return 10.0


def _get_submit_retry_interval_s() -> float:
    # Retry interval for async-submit failures (e.g. transient network issues or backend queue full).
    # Kept configurable for operational tuning; defaults to 20 seconds as requested.
    value = os.environ.get("SPREADSHEET_RL_REWARD_SUBMIT_RETRY_INTERVAL_S", "20")
    try:
        retry_s = float(value)
        return max(0.1, retry_s)
    except ValueError:
        return 20.0


def _is_success_status(status: int) -> bool:
    return 200 <= status < 300


def _is_retryable_http_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _add_query_params(url: str, params: dict[str, str]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    for key, value in params.items():
        query[key] = [value]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _is_async_submit_endpoint(url: str) -> bool:
    try:
        path = urllib.parse.urlsplit(url).path.rstrip("/")
    except Exception:
        return False
    return path.endswith("/submit") or path.endswith("/reward")


def compute_score(ground_truth: Any, extra_info: Any, *, return_msg: bool = False) -> float | Tuple[float, str]:
    """Compute reward by delegating to the external spreadsheet reward API.

    The migrated workflow is:

        - Resolve the workbook root from the rollout workspace
          (`<data_root>/_workspaces/<sandbox_workspace_id>`) when present, otherwise
          from `<data_root>/<thread_dir>`.
        - Prefer `output.xlsx` and fall back to `data.xlsx` so reward computation still
          works when the sandbox only mutated the seeded workbook in place.
        - Submit `thread_dir` plus the chosen workbook bytes to the async reward API.
        - Retry submit/poll on transient failures (`busy`, `429`, `5xx`, connection
          issues) until the overall deadline expires.
        - Clean up the rollout workspace plus `.locks` artifacts in `finally` unless
          `SPREADSHEET_RL_CLEANUP_WORKSPACE=0`.
    """

    def _result(reward: float, msg: str = "") -> float | Tuple[float, str]:
        if return_msg:
            return reward, msg
        return reward

    primary_ext = None
    workspace_id = None
    if isinstance(extra_info, dict):
        primary_ext = extra_info.get("primary_ext")
        workspace_id = normalize_workspace_id(extra_info.get("sandbox_workspace_id"))
    if not isinstance(primary_ext, str) or not primary_ext.strip() or primary_ext.strip().lower() != ".xlsx":
        primary_ext = ".xlsx"
    data_root = get_spreadsheet_rl_data_root()
    workspace_root = (
        data_root / "_workspaces" / workspace_id if isinstance(workspace_id, str) and workspace_id else None
    )

    def _maybe_cleanup_workspace() -> None:
        if workspace_root is None:
            return
        if os.environ.get("SPREADSHEET_RL_CLEANUP_WORKSPACE", "1") == "0":
            return
        workspace_removed = False
        try:
            shutil.rmtree(workspace_root)
            workspace_removed = True
        except FileNotFoundError:
            workspace_removed = True
        except Exception:  # noqa: BLE001
            logger.exception("Failed to clean up workspace directory %s", workspace_root)
            return
        if not workspace_removed:
            return
        try:
            locks_dir = workspace_root.parent / ".locks"
            for filename in (f"{workspace_id}.manifest.json", f"{workspace_id}.lock"):
                try:
                    (locks_dir / filename).unlink()
                except FileNotFoundError:
                    continue
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to clean up workspace lock artifact %s", locks_dir / filename)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to clean up workspace lock directory for %s", workspace_root)

    boundary = uuid.uuid4().hex
    reward_semaphore = _get_reward_semaphore()
    reward_semaphore_acquired = False

    try:
        api_url = os.environ.get("SPREADSHEET_RL_REWARD_URL", "").strip()
        if not api_url:
            return _result(0.0)

        thread_dir = normalize_thread_dir(ground_truth)
        if thread_dir is None:
            return _result(0.0)

        if not isinstance(primary_ext, str) or not primary_ext:
            return _result(0.0)

        if workspace_root is not None:
            full_thread_dir = workspace_root
        else:
            full_thread_dir = data_root / thread_dir
        proc_file = Path(full_thread_dir) / f"output{primary_ext}"

        payload_file = proc_file
        if not payload_file.exists():
            data_file = Path(full_thread_dir) / f"data{primary_ext}"
            if data_file.exists():
                payload_file = data_file

        if not payload_file.exists():
            return _result(0.0)

        filename = payload_file.name
        mime_type = (
            mimetypes.guess_type(filename)[0]
            or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        with payload_file.open("rb") as f:
            file_bytes = f.read()

        if reward_semaphore is not None:
            reward_semaphore.acquire()
            reward_semaphore_acquired = True

        # Build multipart/form-data body with fields:
        #   thread_dir, file
        crlf = b"\r\n"
        lines: list[bytes] = []

        def _add_field(name: str, value: str) -> None:
            lines.append(f"--{boundary}".encode())
            lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
            lines.append(b"")
            lines.append(value.encode())

        _add_field("thread_dir", thread_dir)

        # File field
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode())
        lines.append(f"Content-Type: {mime_type}".encode())
        lines.append(b"")
        lines.append(file_bytes)
        lines.append(f"--{boundary}--".encode())
        lines.append(b"")

        body = crlf.join(lines)

        req = urllib.request.Request(api_url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Content-Length", str(len(body)))

        total_timeout_s = _get_reward_timeout_s()
        deadline = time.monotonic() + total_timeout_s
        submit_timeout_s = total_timeout_s
        if _is_async_submit_endpoint(api_url):
            submit_timeout_s = min(30.0, total_timeout_s)
        submit_retry_interval_s = _get_submit_retry_interval_s()
        payload: dict
        job_id: str
        while True:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                return _result(0.0)

            try:
                with urllib.request.urlopen(req, timeout=min(submit_timeout_s, remaining_s)) as resp:
                    if not _is_success_status(resp.status):
                        if not _is_retryable_http_status(resp.status):
                            return _result(0.0)
                        time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
                        continue
                    resp_data = resp.read().decode()
                payload = json.loads(resp_data)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if not _is_retryable_http_status(status):
                    return _result(0.0)
                time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
                continue
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
                continue

            # Sync API path
            if "reward" in payload:
                try:
                    reward = float(payload.get("reward", 0.0))
                except Exception:
                    reward = 0.0
                msg = payload.get("msg", "")
                return _result(reward, str(msg or ""))

            # Async submit+poll path (retry submit until accepted)
            if payload.get("status") == "busy":
                time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
                continue
            job_id_raw = payload.get("job_id")
            if not isinstance(job_id_raw, str) or not job_id_raw:
                time.sleep(min(submit_retry_interval_s, max(0.0, remaining_s)))
                continue
            job_id = job_id_raw
            break

        result_path = payload.get("result_path")
        if isinstance(result_path, str) and result_path:
            result_url = urllib.parse.urljoin(api_url, result_path)
        else:
            # Best-effort fallback: assume `/reward/submit` → `/reward/result/{job_id}`
            result_url = urllib.parse.urljoin(api_url, f"/reward/result/{job_id}")

        poll_wait_s = _get_poll_wait_s()
        backoff_s = 0.5
        while True:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                return _result(0.0)

            wait_s = min(poll_wait_s, max(0.0, remaining_s))
            poll_url = _add_query_params(result_url, {"wait_s": f"{wait_s:.3f}"})
            poll_timeout_s = min(remaining_s, max(5.0, wait_s + 15.0))
            try:
                with urllib.request.urlopen(poll_url, timeout=poll_timeout_s) as resp:
                    if not _is_success_status(resp.status):
                        if not _is_retryable_http_status(resp.status):
                            return _result(0.0)
                        sleep_s = min(backoff_s, max(0.0, remaining_s))
                        time.sleep(sleep_s)
                        backoff_s = min(backoff_s * 2, 5.0)
                        continue
                    poll_data = resp.read().decode()
                poll_payload = json.loads(poll_data)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if not _is_retryable_http_status(status):
                    return _result(0.0)
                sleep_s = min(backoff_s, max(0.0, remaining_s))
                time.sleep(sleep_s)
                backoff_s = min(backoff_s * 2, 5.0)
                continue
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                sleep_s = min(backoff_s, max(0.0, remaining_s))
                time.sleep(sleep_s)
                backoff_s = min(backoff_s * 2, 5.0)
                continue

            status = poll_payload.get("status")
            if status in {"done", "error"}:
                try:
                    reward = float(poll_payload.get("reward", 0.0))
                except Exception:
                    reward = 0.0
                msg = poll_payload.get("msg", "")
                return _result(reward, str(msg or ""))

            # Still queued/running; if we didn't long-poll, add client-side pacing.
            if poll_wait_s <= 0:
                sleep_s = min(backoff_s, max(0.0, remaining_s))
                time.sleep(sleep_s)
                backoff_s = min(backoff_s * 2, 5.0)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return _result(0.0)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error in sheet arena reward computation")
        return _result(0.0)
    finally:
        if reward_semaphore_acquired and reward_semaphore is not None:
            reward_semaphore.release()
        _maybe_cleanup_workspace()
