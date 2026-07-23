from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from .config import _get_max_upload_bytes
from .messages import format_exception as _format_exception

logger = logging.getLogger(__name__)


class UploadTooLargeError(RuntimeError):
    pass


async def _await_worker_communicate_cleanup(
    communicate_task: asyncio.Task,
    *,
    timeout_s: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + max(0.0, float(timeout_s))
    while not communicate_task.done():
        remaining_s = deadline - asyncio.get_running_loop().time()
        if remaining_s <= 0:
            return
        try:
            await asyncio.wait_for(asyncio.shield(communicate_task), timeout=remaining_s)
        except asyncio.CancelledError:
            continue
        except Exception:
            return


def _copy_upload_to_path(source, destination: Path) -> None:
    max_bytes = _get_max_upload_bytes()
    bytes_copied = 0
    with destination.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            bytes_copied += len(chunk)
            if max_bytes is not None and bytes_copied > max_bytes:
                raise UploadTooLargeError("uploaded file exceeds REWARD_API_MAX_UPLOAD_BYTES")
            output.write(chunk)


async def _await_shielded_ignoring_repeated_cancels(task: asyncio.Task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _run_cancellation_safe(coro):
    task = asyncio.create_task(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await _await_shielded_ignoring_repeated_cancels(task)
        finally:
            raise


async def _copy_upload_to_path_cancellation_safe(source, destination: Path, *, context: str) -> None:
    copy_task = asyncio.create_task(asyncio.to_thread(_copy_upload_to_path, source, destination))
    try:
        await asyncio.shield(copy_task)
    except asyncio.CancelledError:
        try:
            await _await_shielded_ignoring_repeated_cancels(copy_task)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{context}] upload copy failed after request cancellation: {_format_exception(exc)}")
        raise


async def _rmtree_with_retries(path: Path, delays_s: tuple[float, ...]) -> bool:
    for delay_s in delays_s:
        if delay_s:
            await asyncio.sleep(delay_s)
        try:
            await asyncio.to_thread(shutil.rmtree, path)
            return True
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            continue
    return False


async def _unlink_with_retries(path: Path, delays_s: tuple[float, ...]) -> bool:
    for delay_s in delays_s:
        if delay_s:
            await asyncio.sleep(delay_s)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            return True
        except (OSError, ValueError):
            continue
    return False
