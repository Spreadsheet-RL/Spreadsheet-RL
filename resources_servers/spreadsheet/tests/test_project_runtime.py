import pytest
from omegaconf import OmegaConf

from resources_servers.spreadsheet.packaging.prepare_project_runtime import write_config


def test_project_config_overrides_only_cache_location_and_preserves_base(tmp_path):
    base = tmp_path / "base training.yaml"
    original = "policy:\n  train_global_batch_size: 128\nenv:\n  nemo_gym:\n    skip_venv_if_present: true\n"
    base.write_text(original)
    target = tmp_path / "spreadsheet.yaml"
    cache = tmp_path / "project/gym_venvs"
    write_config(target, base, cache)
    result = OmegaConf.to_container(OmegaConf.load(target))
    assert result == {"defaults": str(base), "env": {"nemo_gym": {"uv_venv_dir": str(cache)}}}
    assert target.stat().st_mode & 0o777 == 0o600
    assert base.read_text() == original
    before = target.read_text()
    with pytest.raises(FileExistsError):
        write_config(target, base, cache)
    assert target.read_text() == before
