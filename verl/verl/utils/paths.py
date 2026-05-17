from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Optional

logger = logging.getLogger(__name__)

_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def get_spreadsheet_rl_data_root() -> Path:
    configured = os.environ.get("SPREADSHEET_RL_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    cwd_data = Path.cwd() / "data"
    if cwd_data.is_dir():
        return cwd_data.resolve()
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "data"
        if (parent / "configs").is_dir() and (parent / "scripts" / "multinode_train_fsdp.slurm").is_file():
            return candidate.resolve()
    return (Path(__file__).resolve().parents[3] / "data").resolve()


def normalize_thread_dir(ground_truth: Any) -> Optional[str]:
    """Extract and sanitize a Spreadsheet-RL thread_dir from the dataset ground_truth field.

    Accepts either a string or a single-element list containing a string. Returns a
    normalized POSIX-relative path string, or None if invalid.
    """
    thread_dir: Any = ground_truth
    if isinstance(thread_dir, list) and len(thread_dir) == 1:
        thread_dir = thread_dir[0]
    if not isinstance(thread_dir, str):
        return None
    thread_dir = thread_dir.strip()
    if not thread_dir:
        return None
    if "\0" in thread_dir:
        logger.warning("Invalid thread_dir detected (null byte): %s", thread_dir)
        return None

    path_obj = PurePosixPath(thread_dir)
    if path_obj.is_absolute() or ".." in path_obj.parts:
        logger.warning("Invalid thread_dir detected (potential path traversal): %s", thread_dir)
        return None
    # Avoid workspace self-references like `_workspaces/<id>/...`.
    if path_obj.parts and path_obj.parts[0] == "_workspaces":
        logger.warning("Invalid thread_dir detected (reserved prefix): %s", thread_dir)
        return None

    return str(path_obj)


def normalize_workspace_id(workspace_id: Any) -> Optional[str]:
    """Sanitize a workspace id used as a single directory component."""
    if not isinstance(workspace_id, str):
        return None
    workspace_id = workspace_id.strip()
    if not workspace_id:
        return None
    if "\0" in workspace_id:
        logger.warning("Invalid workspace_id detected (null byte): %s", workspace_id)
        return None

    path_obj = PurePosixPath(workspace_id)
    if path_obj.is_absolute() or ".." in path_obj.parts or len(path_obj.parts) != 1:
        logger.warning("Invalid workspace_id detected (potential path traversal): %s", workspace_id)
        return None
    if not _WORKSPACE_ID_RE.match(workspace_id):
        logger.warning("Invalid workspace_id detected (unsupported characters): %s", workspace_id)
        return None

    return workspace_id
