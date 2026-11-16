"""Build a root-owned, relocatable chroot tree on Ubuntu 24.04 x86_64 only."""

import re
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile


def build(root: Path, wheelhouse: Path):
    root.mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/lib").mkdir(parents=True)
    shutil.copy2("/usr/bin/python3.12", root / "usr/bin/python3.12")
    shutil.copytree(
        "/usr/lib/python3.12",
        root / "usr/lib/python3.12",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.a"),
    )
    packages = root / "usr/lib/python3/dist-packages"
    packages.mkdir(parents=True)
    prefixes = ("openpyxl-", "et_xmlfile-", "numpy-", "pandas-", "python_dateutil-", "six-", "pytz-", "tzdata-")
    for wheel in wheelhouse.glob("*.whl"):
        if wheel.name.startswith(prefixes):
            with ZipFile(wheel) as archive:
                for item in archive.infolist():
                    if not (packages / item.filename).resolve().is_relative_to(packages.resolve()):
                        raise ValueError("Wheel path traversal")
                archive.extractall(packages)
    # Match Ubuntu's usr-merged filesystem without bind mounts.
    for name in ("lib", "lib64", "bin", "sbin"):
        (root / ("usr/" + name)).mkdir(exist_ok=True)
        (root / name).symlink_to("usr/" + name)
    binaries = [root / "usr/bin/python3.12", *root.rglob("*.so"), *root.rglob("*.so.*")]
    copied = set()
    for binary in binaries:
        result = subprocess.run(["ldd", str(binary)], text=True, capture_output=True)
        for dependency in re.findall(r"(?:=>\s+)?(/[^\s()]+)", result.stdout):
            source = Path(dependency)
            if not source.is_file() or source.is_relative_to(root):
                continue
            target = root / source.relative_to("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            copied.add(dependency)
    for name, mode in (("tmp", 0o755), ("workspaces", 0o711)):
        (root / name).mkdir(mode=mode)
    # No setuid executables and no group/world-writable runtime files.
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    (root / "workspaces").chmod(0o711)
    return sorted(copied)
