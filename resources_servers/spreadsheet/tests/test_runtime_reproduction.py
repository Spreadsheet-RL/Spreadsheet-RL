import os
import tarfile

import pytest

from resources_servers.spreadsheet.packaging.verify_runtime import (
    canonical_digest,
    junit_summary,
    pack_bundle,
    repair_bundle,
    tree_manifest,
)


def test_runtime_fingerprint_is_relocatable_but_detects_file_and_mode_changes(tmp_path):
    roots = [tmp_path / name for name in ("a", "different-prefix")]
    for root in roots:
        (root / "usr").mkdir(parents=True)
        (root / "usr/python").write_bytes(b"fixed-runtime")
        (root / "bin").symlink_to("usr")
        (root / "workspaces/job").mkdir(parents=True)
        (root / "workspaces/job/private").write_text(str(root))
    expected = canonical_digest(tree_manifest(roots[0]))
    assert canonical_digest(tree_manifest(roots[1])) == expected
    (roots[1] / "usr/python").write_bytes(b"changed-runtime")
    assert canonical_digest(tree_manifest(roots[1])) != expected
    (roots[1] / "usr/python").write_bytes(b"fixed-runtime")
    (roots[1] / "usr/python").chmod(0o700)
    assert canonical_digest(tree_manifest(roots[1])) != expected


def test_junit_summary_counts_failures_errors_and_skips(tmp_path):
    path = tmp_path / "tests.xml"
    path.write_text(
        '<testsuites><testsuite tests="9" failures="1" errors="2" skipped="1"/></testsuites>'
    )
    assert junit_summary(path) == {
        "tests": 9,
        "failures": 1,
        "errors": 2,
        "skipped": 1,
        "passed": 5,
    }


def test_repaired_archive_is_deterministic_and_excludes_non_relocatable_venv(tmp_path):
    outputs = []
    for index in (1, 2):
        bundle = tmp_path / str(index) / "bundle"
        (bundle / "source").mkdir(parents=True)
        (bundle / "source/app.py").write_text("# pinned source\n")
        os.utime(bundle / "source/app.py", (index, index))
        (bundle / "service-venv").mkdir()
        (bundle / "service-venv/private").write_text(str(bundle))
        target = tmp_path / f"archive{index}.tar.gz"
        pack_bundle(bundle, target)
        outputs.append(target)
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with tarfile.open(outputs[0]) as archive:
        assert "bundle/SHA256SUMS" in archive.getnames()
        assert not any("service-venv" in name for name in archive.getnames())


def test_repaired_archive_refuses_live_sandbox_workspaces(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "python-root/workspaces/live").mkdir(parents=True)
    with pytest.raises(ValueError, match="Live sandbox"):
        pack_bundle(bundle, tmp_path / "must-not-exist.tar.gz")
    assert not (tmp_path / "must-not-exist.tar.gz").exists()


def test_glibc_repair_rejects_unreviewed_differences(tmp_path):
    report = {
        "root_difference_count": 1,
        "root_difference_examples": ["usr/bin/python3.12"],
    }
    with pytest.raises(ValueError, match="four diagnosed"):
        repair_bundle(
            tmp_path / "bundle", tmp_path / "rebuilt", tmp_path, report, "fixed-commit"
        )
    assert not (tmp_path / "runtime-repaired.tar.gz").exists()


def test_glibc_repair_changes_only_diagnosed_files_and_matches_rebuilt_root(tmp_path):
    paths = [
        "usr/lib/x86_64-linux-gnu/libc.so.6",
        "usr/lib/x86_64-linux-gnu/libm.so.6",
        "usr/lib/x86_64-linux-gnu/libpthread.so.0",
        "usr/lib64/ld-linux-x86-64.so.2",
    ]
    bundle, rebuilt = tmp_path / "bundle", tmp_path / "rebuilt"
    for root in (bundle / "python-root", rebuilt):
        for relative in [*paths, "usr/bin/python3.12"]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("aligned")
    for relative in paths:
        (bundle / "python-root" / relative).write_text("old")
    expected = canonical_digest(tree_manifest(rebuilt))
    report = {
        "root_difference_count": 4,
        "root_difference_examples": paths,
        "rebuilt_root_sha256": expected,
        "artifact_sha256": "parent",
        "runtime_fingerprint_sha256": "packages",
        "behavior_sha256": "behavior",
        "tests": {"passed": 34, "skipped": 2},
    }
    result = repair_bundle(bundle, rebuilt, tmp_path, report, "fixed-commit")
    assert result["repaired_root_matches_rebuilt"]
    assert canonical_digest(tree_manifest(bundle / "python-root")) == expected
    assert (bundle / "python-root/usr/bin/python3.12").read_text() == "aligned"
    assert result["repair"]["parent_artifact_sha256"] == "parent"
    assert result["repair"]["changed_runtime_files"] == sorted(paths)
