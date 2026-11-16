import shlex
from pathlib import Path

import pytest

from resources_servers.spreadsheet.packaging import prepare_cached_frontends as cache


def base(root, name):
    path = root / name / ".venv"
    (path / "bin").mkdir(parents=True)
    (path / "bin/python").write_text("fixture interpreter")
    (path / "bin/activate").write_text("fixture activation")
    return path


def test_copy_existing_caches_then_reuse_without_overwriting_changes(tmp_path):
    for source, _ in cache.CACHES:
        base(tmp_path, source)
    result = cache.prepare(tmp_path)
    assert [item["status"] for item in result] == ["copied", "copied"]
    for _, destination in cache.CACHES:
        target = tmp_path / destination / ".venv"
        assert cache.complete(target)
        assert not target.is_symlink()
        (target / "local-evidence").write_text("keep this")
    assert all(item["status"] == "reused" for item in cache.prepare(tmp_path))
    assert all(
        (tmp_path / destination / ".venv/local-evidence").read_text() == "keep this" for _, destination in cache.CACHES
    )


@pytest.mark.parametrize("bad_destination", [False, True])
def test_validate_all_sources_and_targets_before_any_copy(tmp_path, bad_destination):
    base(tmp_path, cache.CACHES[0][0])
    if bad_destination:
        base(tmp_path, cache.CACHES[1][0])
        (tmp_path / cache.CACHES[1][1] / ".venv").mkdir(parents=True)
    with pytest.raises(ValueError, match="incomplete"):
        cache.prepare(tmp_path)
    assert not (tmp_path / cache.CACHES[0][1] / ".venv").exists()


def test_linked_source_root_is_copied_without_creating_a_venv_alias(tmp_path):
    common = base(tmp_path, "common")
    for source, _ in cache.CACHES:
        linked = tmp_path / source / ".venv"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(common, target_is_directory=True)
    (common / "bin/python-alias").symlink_to(Path("python"))
    cache.prepare(tmp_path)
    for _, destination in cache.CACHES:
        target = tmp_path / destination / ".venv"
        assert not target.is_symlink()
        assert (target / "bin/python-alias").is_symlink()


def test_project_caches_include_model_and_activate_the_new_locations(tmp_path):
    source, project = tmp_path / "source", tmp_path / "project cache"
    mappings = cache.CACHES + (("responses_api_models/vllm_model", "responses_api_models/vllm_model"),)
    for original, _ in mappings:
        path = base(source, original)
        (path / "bin/activate").write_text("# fixture\nVIRTUAL_ENV='/original/cache'\nexport VIRTUAL_ENV\n")
    result = cache.prepare(project, source, include_model=True, relocate=True)
    assert len(result) == 3 and all(item["status"] == "copied" for item in result)
    for original, destination in mappings:
        target = project / destination / ".venv"
        assert "VIRTUAL_ENV=" + shlex.quote(str(target)) in (target / "bin/activate").read_text().splitlines()
        assert "VIRTUAL_ENV='/original/cache'" in (source / original / ".venv/bin/activate").read_text()
    assert all(
        item["status"] == "reused" for item in cache.prepare(project, source, include_model=True, relocate=True)
    )


def test_project_cache_rejects_missing_model_before_copying_any_frontend(tmp_path):
    source, target = tmp_path / "source", tmp_path / "project"
    for original, _ in cache.CACHES:
        path = base(source, original)
        (path / "bin/activate").write_text("VIRTUAL_ENV='/original'\n")
    with pytest.raises(ValueError, match="vllm_model"):
        cache.prepare(target, source, include_model=True, relocate=True)
    assert not target.exists()
