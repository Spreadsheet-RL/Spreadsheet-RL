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
