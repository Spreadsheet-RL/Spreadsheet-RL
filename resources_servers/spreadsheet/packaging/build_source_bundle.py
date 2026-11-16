"""Package tracked spreadsheet integration source from a clean Git revision.

The small source overlay contains no runtime dependencies, dataset or credentials.
"""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path


def build(root, output):
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
    if status:
        raise ValueError("Source bundle requires a clean worktree")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    names = (
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "-z",
                "--",
                "resources_servers/spreadsheet",
                "responses_api_agents/spreadsheet_agent",
                "LICENSE",
            ],
            cwd=root,
        )
        .decode()
        .strip("\0")
        .split("\0")
    )
    files = {}
    for name in sorted(names):
        path = root / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix not in {".py", ".json", ".md", ".yaml", ".txt", ".sh", ""}
        ):
            raise ValueError("Unexpected tracked source file in release allowlist")
        files[name] = path.read_bytes()
    manifest = {
        "schema_version": 1,
        "source_commit": commit,
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
        "purpose": "Source overlay; install into an explicitly selected existing Gym checkout",
    }
    files["RELEASE.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    prefix = "spreadsheet-gym-integration-" + commit[:12]
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (prefix + ".tar.gz")
    with archive.open("xb") as target, gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, body in sorted(files.items()):
                info = tarfile.TarInfo(prefix + "/" + name)
                info.size, info.mode, info.mtime = len(body), 0o644, 0
                tar.addfile(info, io.BytesIO(body))
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + ".sha256")).write_text(sha + "  " + archive.name + "\n")
    return {"archive": str(archive), "sha256": sha, "source_commit": commit, "files": len(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parents[3], args.output_dir.resolve())))


if __name__ == "__main__":
    main()
