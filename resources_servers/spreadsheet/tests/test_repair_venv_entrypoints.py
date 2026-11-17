from pathlib import Path
import json
import venv

import pytest

from resources_servers.spreadsheet.packaging.inspect_relocated_runtime import venv_report
from resources_servers.spreadsheet.packaging.repair_venv_entrypoints import apply, plan, restore


def moved_envs(tmp_path):
    rows = []
    for index in range(3):
        old = tmp_path / ("old" + str(index))
        venv.EnvBuilder(with_pip=False, symlinks=True).create(old)
        console = old / "bin/example-console"
        console.write_text("#!" + str(old / "bin/python") + "\nprint(1)\n")
        console.chmod(0o755)
        (old / "bin/wrapped-console").write_text(
            "#!/bin/sh\n'''exec' " + str(old / "bin/python") + " \"$0\" \"$@\"\n' '''\nprint(1)\n"
        )
        new = tmp_path / ("new" + str(index))
        old.rename(new)
        rows.append(venv_report(new))
    return {"cached_venvs": rows}


def test_repair_real_moved_venvs_and_restore_original_bytes(tmp_path):
    report = moved_envs(tmp_path)
    changes = plan(report)
    assert len(changes) >= 12
    receipt_path = apply(report, changes, tmp_path / "backup")
    for row in report["cached_venvs"]:
        assert venv_report(Path(row["venv"]))["activation_matches_location"] is True
    assert all(path.stat().st_mode & 0o111 for path, _, _ in changes if path.name == "example-console")
    restore(json.loads(receipt_path.read_text()))
    assert all(path.read_bytes() == before for path, before, _ in changes)


def test_failed_activation_rolls_back_all_changes(tmp_path, monkeypatch):
    from resources_servers.spreadsheet.packaging import repair_venv_entrypoints as module

    report = moved_envs(tmp_path)
    changes = plan(report)
    monkeypatch.setattr(
        module, "verify_activation", lambda path: (_ for _ in ()).throw(ValueError("activation failed"))
    )
    with pytest.raises(ValueError, match="activation failed"):
        apply(report, changes, tmp_path / "backup")
    assert all(path.read_bytes() == before for path, before, _ in changes)
