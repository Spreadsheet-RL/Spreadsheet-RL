from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _remove_temporary_directory(path: Path) -> None:
    delays_s = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0) if os.name == "nt" else (0.0,)
    last_exc: OSError | None = None
    for delay_s in delays_s:
        if delay_s:
            time.sleep(delay_s)
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc


@contextmanager
def temporary_directory(*, prefix: str) -> Iterator[str]:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield str(path)
    finally:
        body_raised = sys.exc_info()[0] is not None
        try:
            _remove_temporary_directory(path)
        except OSError as exc:
            if not body_raised:
                raise
            print(f"[tempdir] failed to remove {path}: {exc}", file=sys.stderr)
