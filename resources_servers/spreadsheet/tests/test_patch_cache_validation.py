from pathlib import Path
import subprocess

import pytest

from resources_servers.spreadsheet.packaging.patch_cache_validation import ANCHOR, patch
from resources_servers.spreadsheet.packaging import validate_existing_cache as cache_checker
from omegaconf import OmegaConf


def test_generated_launcher_skips_fatal_judge_copy_and_fails_closed_on_bad_cache(tmp_path):
    # Reproduce a launcher whose unrelated copy helper always fails first.
    validator = tmp_path / "validator.py"
    validator.write_text("raise SystemExit(3)\n")
    original = "#!/bin/bash\n# Spreadsheet full-training IPC\n" + ANCHOR + "\necho TRAINING_STARTED\n"
    patched = patch(original, validator, tmp_path)
    assert ANCHOR not in patched
    # Use the current interpreter; no packages/network or training involved.
    import sys

    patched = patched.replace("python -B", "'" + sys.executable + "' -B")
    result = subprocess.run(["bash"], input=patched, text=True, capture_output=True)
    assert result.returncode == 3
    assert "TRAINING_STARTED" not in result.stdout
    validator.write_text("raise SystemExit(0)\n")
    result = subprocess.run(["bash"], input=patched, text=True, capture_output=True)
    assert result.returncode == 0 and "TRAINING_STARTED" in result.stdout
    with pytest.raises(ValueError):
        patch(ANCHOR, Path("validator"), tmp_path)


def test_cache_check_reuses_exactly_three_envs_and_rejects_wrong_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_checker.metadata, "version", lambda name: "fixture-version")
    config = OmegaConf.create({"env": {"nemo_gym": {"uv_venv_dir": str(tmp_path), "skip_venv_if_present": True}}})
    calls = []

    def probe(path, root, module):
        calls.append(path)
        return {
            "prefix": str(path),
            "venv_path": str(path),
            "versions": {"ray": "fixture-version", "openai": "fixture-version"},
        }

    cache_checker.validate(config, tmp_path, probe)
    assert len(calls) == 3
    assert not any("math_with_judge" in str(path) for path in calls)

    def wrong(path, root, module):
        result = probe(path, root, module)
        result["prefix"] = "/wrong/venv"
        return result

    with pytest.raises(ValueError, match="different venv"):
        cache_checker.validate(config, tmp_path, wrong)
