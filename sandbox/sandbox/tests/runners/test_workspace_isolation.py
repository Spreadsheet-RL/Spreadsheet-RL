# Copyright 2025 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sandbox.runners.base import (
    _copy_workspace_paths,
    _expose_bind_mounted_data_dir,
    _normalize_workspace_paths,
    _prepare_workspace_data_root,
    _sanitize_relpath,
    _sanitize_workspace_id,
)
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus, sandbox_router

app = FastAPI()
app.include_router(sandbox_router, tags=["sandbox"])
client = TestClient(app)


def test_sanitize_workspace_id():
    assert _sanitize_workspace_id("abc") == "abc"
    assert _sanitize_workspace_id("A1_.-") == "A1_.-"
    assert _sanitize_workspace_id("_bad") is None
    assert _sanitize_workspace_id(" has space ") is None
    assert _sanitize_workspace_id("") is None
    assert _sanitize_workspace_id("a" * 129) is None


def test_sanitize_relpath():
    assert _sanitize_relpath("excelforum/a/b") == os.path.join("excelforum", "a", "b")
    assert _sanitize_relpath("foo/bar") == os.path.join("foo", "bar")
    assert _sanitize_relpath("/abs/path") is None
    assert _sanitize_relpath("../escape") is None
    assert _sanitize_relpath("a/../../b") is None
    assert _sanitize_relpath("safe/path\0/../escape") is None
    assert _sanitize_relpath("") is None


def test_copy_workspace_paths_copies_regular_file_and_dir(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace_dir = tmp_path / "workspace"
    data_root.mkdir()
    workspace_dir.mkdir()

    (data_root / "foo.txt").write_text("hello", encoding="utf-8")
    (data_root / "dir").mkdir()
    (data_root / "dir" / "bar.txt").write_text("world", encoding="utf-8")

    _copy_workspace_paths(str(workspace_dir), str(data_root), _normalize_workspace_paths(["foo.txt", "dir"]))

    assert (workspace_dir / "foo.txt").read_text(encoding="utf-8") == "hello"
    assert (workspace_dir / "dir" / "bar.txt").read_text(encoding="utf-8") == "world"


def test_copy_workspace_paths_overwrites_existing_destination(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace_dir = tmp_path / "workspace"
    data_root.mkdir()
    workspace_dir.mkdir()

    (data_root / "foo.txt").write_text("hello", encoding="utf-8")
    (workspace_dir / "foo.txt").write_text("stale", encoding="utf-8")

    _copy_workspace_paths(str(workspace_dir), str(data_root), _normalize_workspace_paths(["foo.txt"]))

    assert (workspace_dir / "foo.txt").read_text(encoding="utf-8") == "hello"


def test_copy_workspace_paths_skips_symlink_escape(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace_dir = tmp_path / "workspace"
    outside = tmp_path / "outside"
    data_root.mkdir()
    workspace_dir.mkdir()
    outside.mkdir()

    secret = outside / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    (data_root / "leak").symlink_to(secret)

    _copy_workspace_paths(str(workspace_dir), str(data_root), _normalize_workspace_paths(["leak"]))

    assert not (workspace_dir / "leak").exists()


def test_copy_workspace_paths_skips_symlink_inside_tree(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace_dir = tmp_path / "workspace"
    outside = tmp_path / "outside"
    data_root.mkdir()
    workspace_dir.mkdir()
    outside.mkdir()

    secret = outside / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")

    (data_root / "dir").mkdir()
    (data_root / "dir" / "ok.txt").write_text("ok", encoding="utf-8")
    (data_root / "dir" / "leak").symlink_to(secret)

    _copy_workspace_paths(str(workspace_dir), str(data_root), _normalize_workspace_paths(["dir"]))

    assert (workspace_dir / "dir" / "ok.txt").read_text(encoding="utf-8") == "ok"
    assert not (workspace_dir / "dir" / "leak").exists()


def test_copy_workspace_paths_flattens_output_xlsx_to_data_xlsx(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace_dir = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (data_root / "threads" / "t1").mkdir(parents=True)
    workspace_dir.mkdir()
    outside.mkdir()

    payload = b"workbook-bytes"
    (data_root / "threads" / "t1" / "output.xlsx").write_bytes(payload)

    secret = outside / "secret.txt"
    secret.write_text("do-not-overwrite", encoding="utf-8")
    (workspace_dir / "data.xlsx").symlink_to(secret)

    _copy_workspace_paths(
        str(workspace_dir),
        str(data_root),
        _normalize_workspace_paths(["threads/t1/output.xlsx"]),
    )

    assert not (workspace_dir / "data.xlsx").is_symlink()
    assert (workspace_dir / "data.xlsx").read_bytes() == payload
    assert secret.read_text(encoding="utf-8") == "do-not-overwrite"
    assert not (workspace_dir / "threads").exists()


def test_copy_workspace_paths_flattens_output_xlsx_overwrites_symlink_to_directory(tmp_path: Path):
    data_root = tmp_path / "data"
    workspace_dir = tmp_path / "workspace"
    outside_dir = tmp_path / "outside_dir"
    (data_root / "threads" / "t1").mkdir(parents=True)
    workspace_dir.mkdir()
    outside_dir.mkdir()

    payload = b"workbook-bytes"
    (data_root / "threads" / "t1" / "output.xlsx").write_bytes(payload)
    (workspace_dir / "data.xlsx").symlink_to(outside_dir)

    _copy_workspace_paths(
        str(workspace_dir),
        str(data_root),
        _normalize_workspace_paths(["threads/t1/output.xlsx"]),
    )

    assert outside_dir.is_dir()
    assert not (workspace_dir / "data.xlsx").is_symlink()
    assert (workspace_dir / "data.xlsx").read_bytes() == payload


def test_expose_bind_mounted_data_dir_override_keeps_data_dir_as_directory(tmp_path: Path):
    cwd = tmp_path / "cwd"
    data_root = tmp_path / "data_root"
    cwd.mkdir()
    data_root.mkdir()
    (data_root / "data.xlsx").write_text("content", encoding="utf-8")

    _expose_bind_mounted_data_dir(str(cwd), data_root_override=str(data_root))

    local_data_dir = cwd / "data"
    assert local_data_dir.is_dir()
    assert not local_data_dir.is_symlink()
    assert (local_data_dir / "data.xlsx").is_symlink()


def test_prepare_workspace_data_root_invalid_workspace_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("SANDBOX_DATA_ROOT", str(data_root))
    monkeypatch.delenv("SANDBOX_WORKSPACE_ROOT", raising=False)

    assert _prepare_workspace_data_root("../bad", ["foo"]) is None


def test_prepare_workspace_data_root_copies_requested_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("SANDBOX_DATA_ROOT", str(data_root))
    monkeypatch.delenv("SANDBOX_WORKSPACE_ROOT", raising=False)

    (data_root / "dir").mkdir()
    (data_root / "dir" / "file.txt").write_text("content", encoding="utf-8")

    workspace_dir = _prepare_workspace_data_root("ws1", ["dir"])
    assert workspace_dir is not None
    assert Path(workspace_dir).is_dir()
    assert (Path(workspace_dir) / "dir" / "file.txt").read_text(encoding="utf-8") == "content"


def test_prepare_workspace_data_root_skips_recopies_when_manifest_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("SANDBOX_DATA_ROOT", str(data_root))
    monkeypatch.delenv("SANDBOX_WORKSPACE_ROOT", raising=False)

    (data_root / "dir").mkdir()
    (data_root / "dir" / "file.txt").write_text("content", encoding="utf-8")

    workspace_dir = _prepare_workspace_data_root("ws1", ["dir"])
    assert workspace_dir is not None

    workspace_file = Path(workspace_dir) / "dir" / "file.txt"
    workspace_file.write_text("mutated", encoding="utf-8")

    workspace_dir_again = _prepare_workspace_data_root("ws1", ["dir"])
    assert workspace_dir_again == workspace_dir
    assert workspace_file.read_text(encoding="utf-8") == "mutated"

    manifest_path = data_root / "_workspaces" / ".locks" / "ws1.manifest.json"
    assert manifest_path.is_file()


def test_run_code_request_requires_workspace_id_and_paths_together():
    with pytest.raises(ValidationError):
        RunCodeRequest(language="python", code="print(1)", workspace_id="ws1", workspace_paths=[])

    with pytest.raises(ValidationError):
        RunCodeRequest(language="python", code="print(1)", workspace_id=None, workspace_paths=["foo"])

    RunCodeRequest(language="python", code="print(1)")
    RunCodeRequest(language="python", code="print(1)", workspace_id="ws1", workspace_paths=["foo"])


def test_run_code_workspace_isolation_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_root = tmp_path / "data_root"
    (data_root / "threads" / "t1").mkdir(parents=True)
    (data_root / "threads" / "t1" / "input.txt").write_text("original", encoding="utf-8")

    monkeypatch.setenv("SANDBOX_DATA_ROOT", str(data_root))
    monkeypatch.delenv("SANDBOX_WORKSPACE_ROOT", raising=False)

    code = r"""
set -euo pipefail
orig="$(cat data/threads/t1/input.txt)"
echo "orig=${orig}"
echo "mutated" > data/threads/t1/input.txt
after="$(cat data/threads/t1/input.txt)"
echo "after=${after}"
"""

    request = RunCodeRequest(
        language="bash",
        code=code,
        run_timeout=5,
        workspace_id="ws1",
        workspace_paths=["threads/t1"],
    )
    response = client.post("/run_code", json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert "orig=original" in (result.run_result.stdout or "")
    assert "after=mutated" in (result.run_result.stdout or "")

    # Original bind-mounted dataset remains unchanged.
    assert (data_root / "threads" / "t1" / "input.txt").read_text(encoding="utf-8") == "original"
    # Workspace copy is writable and reflects the mutation.
    assert (data_root / "_workspaces" / "ws1" / "threads" / "t1" / "input.txt").read_text(encoding="utf-8").strip() == "mutated"


def test_run_code_workspace_isolation_flattens_output_xlsx_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data_root = tmp_path / "data_root"
    (data_root / "threads" / "t1").mkdir(parents=True)
    (data_root / "threads" / "t1" / "output.xlsx").write_text("original", encoding="utf-8")

    monkeypatch.setenv("SANDBOX_DATA_ROOT", str(data_root))
    monkeypatch.delenv("SANDBOX_WORKSPACE_ROOT", raising=False)

    code = r"""
set -euo pipefail
echo "entries_before=$(ls -1 data | tr '\n' ',' )"
echo "before=$(cat data/data.xlsx)"
echo "mutated" > data/data.xlsx
mv data/data.xlsx data/output.xlsx
echo "entries_after=$(ls -1 data | tr '\n' ',' )"
echo "after=$(cat data/output.xlsx)"
"""

    request = RunCodeRequest(
        language="bash",
        code=code,
        run_timeout=5,
        workspace_id="ws_flat",
        workspace_paths=["threads/t1/output.xlsx"],
    )
    response = client.post("/run_code", json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    stdout = result.run_result.stdout or ""
    assert "before=original" in stdout
    assert "after=mutated" in stdout
    assert "entries_before=data.xlsx," in stdout

    assert (data_root / "threads" / "t1" / "output.xlsx").read_text(encoding="utf-8") == "original"
    assert (data_root / "_workspaces" / "ws_flat" / "output.xlsx").read_text(encoding="utf-8").strip() == "mutated"
