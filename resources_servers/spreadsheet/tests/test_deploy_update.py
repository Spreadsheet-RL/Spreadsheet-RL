import json
import sqlite3

import pytest

from resources_servers.spreadsheet.packaging.deploy_service_update import FILES, atomic, digest, idle, validate_overlay
from resources_servers.spreadsheet.service.app import ServiceConfig


def overlay(tmp_path):
    patches, source = tmp_path / "patches", tmp_path / "source"
    patches.mkdir()
    manifest = {"source_commit": "reviewed-commit", "files": {}, "previous": {}}
    for name, relative in FILES.items():
        old = source / relative
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("value = 1\n")
        new = patches / name
        new.write_text("value = 2\n")
        manifest["files"][name], manifest["previous"][name] = digest(new), digest(old)
    (patches / "PATCH.json").write_text(json.dumps(manifest))
    return patches, source


def test_only_pinned_three_file_overlay_is_accepted(tmp_path):
    patches, source = overlay(tmp_path)
    assert validate_overlay(patches, source)["source_commit"] == "reviewed-commit"
    (patches / "workbook.py").write_text("value = 3\n")
    with pytest.raises(ValueError, match="hash differs"):
        validate_overlay(patches, source)


def test_existing_service_edits_are_preserved(tmp_path):
    patches, source = overlay(tmp_path)
    (source / FILES["state.py"]).write_text("operator_edit = True\n")
    with pytest.raises(ValueError, match="hash differs"):
        validate_overlay(patches, source)


def test_overlay_rejects_additional_files(tmp_path):
    patches, source = overlay(tmp_path)
    manifest = json.loads((patches / "PATCH.json").read_text())
    manifest["files"]["unreviewed.py"] = "x"
    (patches / "PATCH.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Only the three"):
        validate_overlay(patches, source)


def test_atomic_config_restore_preserves_exact_bytes_and_mode(tmp_path):
    path = tmp_path / "config.json"
    before = b'{"state_root":"unchanged"}\n'
    path.write_bytes(before)
    path.chmod(0o600)
    atomic(path, b'{"state_root":"unchanged","max_pending":4000}\n')
    atomic(path, before)
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600


def test_open_sessions_and_pending_jobs_prevent_service_stop(tmp_path):
    (tmp_path / "sessions").mkdir()
    database = tmp_path / "sessions/state.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE jobs(status TEXT); CREATE TABLE sessions(state TEXT);")
        db.execute("INSERT INTO sessions VALUES ('OPEN')")
    with pytest.raises(ValueError, match="no process was stopped"):
        idle(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE sessions SET state='CLOSED'")
        db.execute("INSERT INTO jobs VALUES ('RUNNING')")
    with pytest.raises(ValueError, match="no process was stopped"):
        idle(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE jobs SET status='DONE'")
    idle(tmp_path)


def test_service_queue_default_is_4000(tmp_path):
    config = ServiceConfig(manifest=tmp_path / "manifest", data_root=tmp_path, state_root=tmp_path,
                           api_key_file=tmp_path / "key")
    assert config.max_pending == 4000
