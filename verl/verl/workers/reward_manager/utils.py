from __future__ import annotations

from typing import Any


def merge_sandbox_workspace_id(extra_info: Any, *, sandbox_workspace_id: Any = None, tool_extra_fields: Any = None) -> dict:
    """Return a (shallow) dict copy of extra_info with sandbox workspace info merged in.

    This is used for per-rollout filesystem isolation (e.g., Spreadsheet-RL rollouts using SandboxFusion)
    where each rollout writes outputs under `data/_workspaces/<sandbox_workspace_id>/...`.
    """
    merged = dict(extra_info) if isinstance(extra_info, dict) else {}

    workspace_id = sandbox_workspace_id
    if workspace_id is None:
        tool_fields = tool_extra_fields
        if tool_fields is not None and not isinstance(tool_fields, (dict, str, bytes)):
            try:
                if len(tool_fields) == 1:
                    tool_fields = tool_fields[0]
            except Exception:  # noqa: BLE001
                tool_fields = tool_extra_fields

        if isinstance(tool_fields, dict):
            workspace_id = tool_fields.get("sandbox_workspace_id")

    if isinstance(workspace_id, str) and workspace_id:
        merged["sandbox_workspace_id"] = workspace_id

    return merged
