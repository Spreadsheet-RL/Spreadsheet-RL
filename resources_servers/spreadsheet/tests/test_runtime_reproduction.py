from resources_servers.spreadsheet.packaging.verify_runtime import canonical_digest, junit_summary, tree_manifest


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
    path.write_text('<testsuites><testsuite tests="9" failures="1" errors="2" skipped="1"/></testsuites>')
    assert junit_summary(path) == {"tests": 9, "failures": 1, "errors": 2, "skipped": 1, "passed": 5}
