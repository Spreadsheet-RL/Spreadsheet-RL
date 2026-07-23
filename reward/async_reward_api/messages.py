from __future__ import annotations

import re


_PATH_WITH_EXTENSION_RE = re.compile(
    r"(?ix)"
    r"(?<![\w])"
    r"(?:"
    r"[a-z]:[\\/][^\r\n\"'<>|]*?"
    r"|\\\\[^\r\n\"'<>|]*?"
    r"|/(?:tmp|temp|var|home|users|mnt|workspace|workspaces|app|opt|root)[^\r\n\"'<>|]*?"
    r")"
    r"\.(?:xlsx|xlsm|xls|csv|json|sqlite3|db|txt|log|py)\b"
)

_PATH_RE = re.compile(
    r"(?ix)"
    r"(?<![\w])"
    r"(?:"
    r"[a-z]:[\\/][^\s\"'<>|]+"
    r"|\\\\[^\s\"'<>|]+(?:[\\/][^\s\"'<>|]+)+"
    r"|/(?:tmp|temp|var|home|users|mnt|workspace|workspaces|app|opt|root)[^\s\"'<>|]*"
    r")"
)

_ABSOLUTE_PATH_TO_LINE_END_RE = re.compile(
    r"(?ix)"
    r"(?<![\w])"
    r"(?:"
    r"[a-z]:[\\/](?:(?!\s*:\s)[^\r\n\"'<>|])*"
    r"|\\\\(?:(?!\s*:\s)[^\r\n\"'<>|])*"
    r"|/(?:tmp|temp|var|home|users|mnt|workspace|workspaces|app|opt|root)(?:(?!\s*:\s)[^\r\n\"'<>|])*"
    r")"
)


def format_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def redact_paths(text: str) -> str:
    text = _PATH_WITH_EXTENSION_RE.sub("<path>", text)
    text = _ABSOLUTE_PATH_TO_LINE_END_RE.sub("<path>", text)
    return _PATH_RE.sub("<path>", text)


def public_worker_message(msg: object, *, fallback: str) -> str:
    text = " ".join(str(msg or "").split())
    if not text:
        return fallback
    text = redact_paths(text)
    if len(text) > 500:
        return f"{text[:497]}..."
    return text
