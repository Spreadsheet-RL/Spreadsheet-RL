import json
from pathlib import Path
import sqlite3

import pytest

from resources_servers.spreadsheet.packaging.relocate_live_service import idle, moved_config, replace_config


def test_only_two_verified_dataset_fields_change():
    original = dict(
        data_root="/workspace/storage-shared/nlp/tungks/data/workbooks",
        manifest="/workspace/storage-shared/nlp/tungks/data/catalog.jsonl",
        state_root="/opt/state",
        python_rootfs="/opt/python-root",
        api_key_file="/opt/private/api-key",
    )
    result = moved_config(
        original,
        Path("/workspace/storage-shared/nlp/users/tungks/data/workbooks"),
        Path("/workspace/storage-shared/nlp/users/tungks/data/catalog.jsonl"),
    )
    assert {key for key in original if original[key] != result[key]} == {"data_root", "manifest"}
    assert original["data_root"].startswith("/workspace/storage-shared/nlp/tungks/")
    with pytest.raises(ValueError):
        moved_config(original, Path("/other/data"), Path("/other/manifest"))


def test_config_backup_restore_preserves_bytes_and_permissions(tmp_path):
    config = tmp_path / "service.json"
    body = b'{"data_root": "old"}\n'
    config.write_bytes(body)
    config.chmod(0o600)
    replace_config(config, json.dumps({"data_root": "new"}).encode())
    replace_config(config, body)
    assert config.read_bytes() == body
    assert config.stat().st_mode & 0o777 == 0o600


def test_active_jobs_prevent_restart(tmp_path):
    folder = tmp_path / "sessions"
    folder.mkdir()
    with sqlite3.connect(folder / "state.sqlite3") as db:
        db.execute("CREATE TABLE jobs(status TEXT)")
        db.execute("INSERT INTO jobs VALUES ('RUNNING')")
    with pytest.raises(ValueError, match="no process was stopped"):
        idle(tmp_path)
    with sqlite3.connect(folder / "state.sqlite3") as db:
        db.execute("UPDATE jobs SET status='DONE'")
    idle(tmp_path)
