"""Reproduce a pinned service runtime offline in a disposable Ubuntu container.

This installs Ubuntu packages: never run install mode on a training node or Mac.
Probe and inventory modes use the installer-created Python, not the host SDK.
"""

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tree_manifest(root):
    """Content/modes/links; exclude mutable workspaces and Python bytecode."""
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in {"tmp", "workspaces"} or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        info = path.lstat()
        entry = {"mode": stat.S_IMODE(info.st_mode)}
        if path.is_symlink():
            entry.update(kind="symlink", target=os.readlink(path))
        elif path.is_dir():
            entry.update(kind="directory")
        elif path.is_file():
            entry.update(kind="file", sha256=digest(path))
        else:
            raise ValueError("Unexpected special file in Python root")
        result[str(relative)] = entry
    return result


def junit_summary(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {
        key: sum(int(suite.get(key, 0)) for suite in suites) for key in ("tests", "failures", "errors", "skipped")
    }
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return counts


def archive_paths(bundle):
    workspace_root = bundle / "python-root/workspaces"
    if workspace_root.exists() and any(workspace_root.iterdir()):
        raise ValueError("Live sandbox workspaces must not enter an artifact")
    for path in sorted(bundle.rglob("*")):
        parts = path.relative_to(bundle).parts
        if "service-venv" in parts or ".pytest_cache" in parts or "__pycache__" in parts or path.suffix == ".pyc":
            continue
        yield path


def pack_bundle(bundle, archive):
    paths = list(archive_paths(bundle))
    sums = [
        digest(path) + "  " + str(path.relative_to(bundle))
        for path in paths
        if path.is_file() and not path.is_symlink() and path != bundle / "SHA256SUMS"
    ]
    (bundle / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    paths = [bundle, *archive_paths(bundle)]
    with (
        archive.open("xb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0, compresslevel=3) as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in paths:
                info = tar.gettarinfo(str(path), arcname=str(Path(bundle.name) / path.relative_to(bundle)))
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.pax_headers = {}
                if path.is_file() and not path.is_symlink():
                    with path.open("rb") as stream:
                        tar.addfile(info, stream)
                else:
                    tar.addfile(info)
    return {"sha256": digest(archive), "bytes": archive.stat().st_size}


def repair_bundle(bundle, rebuilt, work, report, source_commit):
    allowed = {
        "usr/lib/x86_64-linux-gnu/libc.so.6",
        "usr/lib/x86_64-linux-gnu/libm.so.6",
        "usr/lib/x86_64-linux-gnu/libpthread.so.0",
        "usr/lib64/ld-linux-x86-64.so.2",
    }
    if report["root_difference_count"] != 4 or set(report["root_difference_examples"]) != allowed:
        raise ValueError("Repair is limited to the four diagnosed glibc/loader files")
    for relative in sorted(allowed):
        source, target = rebuilt / relative, bundle / "python-root" / relative
        if source.is_symlink() or target.is_symlink() or not source.is_file() or not target.is_file():
            raise ValueError("Expected regular glibc files")
        shutil.copy2(source, target)
    repaired_sha = canonical_digest(tree_manifest(bundle / "python-root"))
    assert repaired_sha == report["rebuilt_root_sha256"]
    metadata = {
        "kind": "align_python_root_with_locked_offline_debs",
        "parent_artifact_sha256": report["artifact_sha256"],
        "repair_source_commit": source_commit,
        "changed_runtime_files": sorted(allowed),
        "root_sha256": repaired_sha,
        "runtime_fingerprint_sha256": report["runtime_fingerprint_sha256"],
        "behavior_sha256": report["behavior_sha256"],
        "tests": report["tests"],
    }
    (bundle / "runtime-reproduction.json").write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")
    result = pack_bundle(bundle, work / "runtime-repaired.tar.gz")
    return {"repaired_root_matches_rebuilt": True, "repaired_archive": result, "repair": metadata}


def run(argv, log, *, cwd=None, env=None, timeout=600):
    with log.open("w") as stream:
        completed = subprocess.run(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"Command failed with exit {completed.returncode}; see {log.name}")


def inventory(bundle):
    import importlib.metadata

    debs = {}
    for path in sorted((bundle / "debs").glob("*.deb")):
        fields = subprocess.check_output(["dpkg-deb", "-f", str(path), "Package", "Version"], text=True)
        control = dict(line.split(": ", 1) for line in fields.splitlines())
        name, version = control["Package"], control["Version"]
        installed = subprocess.check_output(["dpkg-query", "-W", "-f", "${Version}", name], text=True)
        if installed != version:
            raise RuntimeError(f"Installed Ubuntu package differs from artifact: {name}")
        debs[name] = {"version": version, "sha256": digest(path)}
    packages = {dist.metadata["Name"].lower(): dist.version for dist in importlib.metadata.distributions()}
    critical = [
        Path("/usr/bin/python3.12"),
        Path("/usr/lib/libreoffice/program/soffice.bin"),
        Path("/usr/lib/python3/dist-packages/uno.py"),
        Path("/usr/lib/x86_64-linux-gnu/libseccomp.so.2"),
    ]
    return {
        "python": platform.python_version(),
        "glibc": list(platform.libc_ver()),
        "libreoffice": subprocess.check_output(["soffice", "--version"], text=True).strip(),
        "debs": debs,
        "python_distributions": packages,
        "critical_files": {str(path): digest(path) for path in critical},
    }


def probe(bundle, work):
    sys.path.insert(0, str(bundle / "source"))
    from openpyxl import Workbook, load_workbook

    from resources_servers.spreadsheet.packaging.make_python_root import build
    from resources_servers.spreadsheet.service.chroot_runner import ChrootPython
    from resources_servers.spreadsheet.service.engines import LibreOfficePool
    from resources_servers.spreadsheet.service.workbook import compare

    rebuilt = work / "rebuilt-python-root"
    libraries = build(rebuilt, bundle / "wheelhouse")
    published_tree, rebuilt_tree = tree_manifest(bundle / "python-root"), tree_manifest(rebuilt)
    differences = [
        path
        for path in sorted(published_tree.keys() | rebuilt_tree.keys())
        if published_tree.get(path) != rebuilt_tree.get(path)
    ]
    runner = ChrootPython(rebuilt, timeout=30)
    runner.probe()
    data = work / "behavior"
    data.mkdir()
    for name, value in (("seed.xlsx", 0), ("oracle.xlsx", 42)):
        workbook = Workbook()
        workbook.active.title = "Entries"
        workbook.active["A1"] = 21
        workbook.create_sheet("Summary")["B2"] = value
        workbook.save(data / name)
        workbook.close()
    seed, oracle = data / "seed.xlsx", data / "oracle.xlsx"
    hashes = [digest(seed), digest(oracle)]
    workspace = data / "rollout-a"
    workspace.mkdir()
    shutil.copyfile(seed, workspace / "data.xlsx")
    wrong = compare(oracle, workspace / "data.xlsx", "Summary!B2")
    code = (
        "import os,socket\n"
        "assert os.geteuid() >= 10000\n"
        "assert not os.path.exists('/proc/self/root')\n"
        f"assert not os.path.exists({str(oracle)!r})\n"
        "try:\n socket.socket()\n"
        "except PermissionError:\n pass\n"
        "else:\n raise AssertionError('sandbox network allowed')\n"
        "from openpyxl import load_workbook\n"
        "w=load_workbook('data.xlsx')\n"
        "w['Summary']['B2']='=Entries!A1*2'\n"
        "w.save('data.xlsx')\n"
        "print('isolated-edit-ok')\n"
    )
    assert "isolated-edit-ok" in runner.execute(workspace, code)["stdout"]
    office = LibreOfficePool(work / "office", workers=1, timeout=120)
    try:
        office.probe()
        office.recalculate(workspace / "data.xlsx")
        correct = compare(oracle, workspace / "data.xlsx", "Summary!B2")
        workbook = load_workbook(workspace / "data.xlsx", data_only=True)
        value = workbook["Summary"]["B2"].value
        workbook.close()
        behavior = {
            "engine": office.version,
            "wrong": wrong,
            "correct": correct,
            "recalculated_value": value,
            "python_isolation": "chroot_uid_seccomp",
            "originals_unchanged": hashes == [digest(seed), digest(oracle)],
        }
    finally:
        office.close()
    assert wrong["reward"] == 0 and correct["reward"] == 1 and value == 42 and behavior["originals_unchanged"]
    return {
        "published_root_sha256": canonical_digest(published_tree),
        "rebuilt_root_sha256": canonical_digest(rebuilt_tree),
        "root_file_count": len(rebuilt_tree),
        "rebuilt_matches_published": not differences,
        "root_difference_examples": differences[:12],
        "root_difference_count": len(differences),
        "rootfs_libraries": libraries,
        "behavior": behavior,
        "behavior_sha256": canonical_digest(behavior),
    }


def install(archive, lock, work, repair_commit=None):
    if work.exists():
        raise ValueError("Select a fresh work directory")
    work.mkdir(mode=0o700, parents=True)
    report = {"checks_passed": False, "hostname": socket.gethostname(), "phase": "artifact"}
    started = time.monotonic()
    try:
        artifact = lock["artifact"]
        if archive.stat().st_size != artifact["bytes"] or digest(archive) != artifact["sha256"]:
            raise ValueError("Runtime artifact checksum/size mismatch")
        report["artifact_sha256"] = artifact["sha256"]
        try:
            connection = socket.create_connection(("1.1.1.1", 443), timeout=2)
        except OSError:
            report["external_network_probe_blocked"] = True
        else:
            connection.close()
            raise RuntimeError("Verifier must run with external networking blocked")
        destination = work / "unpacked"
        destination.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(destination, filter="data")
        bundle = destination / "spreadsheet-gym-ubuntu24.04-x86_64"
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(work / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_NO_INDEX": "1",
            "PYTHONPATH": str(bundle / "source"),
        }
        Path(env["HOME"]).mkdir()
        report["phase"] = "offline_install"
        run(["bash", str(bundle / "install_offline.sh")], work / "install.log", env=env)
        python = str(bundle / "service-venv/bin/python")
        script = str(Path(__file__).resolve())
        core = json.loads(
            subprocess.check_output([python, "-I", script, "inventory", "--bundle", str(bundle)], env=env)
        )
        report["runtime_fingerprint"] = core
        report["runtime_fingerprint_sha256"] = canonical_digest(core)
        if core["libreoffice"] != lock["expected"]["libreoffice"]:
            raise RuntimeError("LibreOffice version differs from lock")
        report["phase"] = "offline_test_dependencies"
        run(
            [
                python,
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(bundle / "wheelhouse"),
                "pytest==9.0.3",
                "httpx==0.28.1",
            ],
            work / "test-dependencies.log",
            env=env,
        )
        report["phase"] = "rebuild_python_root_and_probe"
        probe_result = json.loads(
            subprocess.check_output(
                [python, "-I", script, "probe", "--bundle", str(bundle), "--work", str(work)], env=env, timeout=240
            )
        )
        report.update(probe_result)
        report["phase"] = "runtime_tests"
        env.update(SPREADSHEET_TEST_REAL_LO="1", SPREADSHEET_TEST_ROOTFS=str(work / "rebuilt-python-root"))
        run(
            [
                python,
                "-m",
                "pytest",
                "-c",
                "/dev/null",
                "-q",
                "-ra",
                "--junitxml=" + str(work / "tests.xml"),
                "resources_servers/spreadsheet/tests",
            ],
            work / "tests.log",
            cwd=bundle / "source",
            env=env,
            timeout=240,
        )
        report["tests"] = junit_summary(work / "tests.xml")
        expected = lock["expected"]
        tests_match = (
            report["tests"]["passed"] == expected["tests_passed"]
            and report["tests"]["skipped"] == expected["tests_skipped"]
        )
        if repair_commit:
            if not tests_match:
                raise ValueError("Do not repair an unqualified runtime")
            report.update(repair_bundle(bundle, work / "rebuilt-python-root", work, report, repair_commit))
        report["checks_passed"] = (
            report["rebuilt_matches_published"] or report.get("repaired_root_matches_rebuilt", False)
        ) and tests_match
        report["phase"] = "complete"
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    report["elapsed_seconds"] = time.monotonic() - started
    (work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("install", "inventory", "probe"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--repair-commit")
    args = parser.parse_args()
    if args.mode == "inventory":
        result = inventory(args.bundle)
    elif args.mode == "probe":
        result = probe(args.bundle, args.work)
    else:
        result = install(args.archive, json.loads(args.lock.read_text()), args.work, args.repair_commit)
    print(json.dumps(result), flush=True)
    if args.mode == "install" and not result["checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
