"""CPU-only qualification and offline bundle build. Invoke with explicit profile.

modal run --profile kieusontung8 .../modal_build.py --code-commit COMMIT
No HF upload occurs without --publish. The HF token is passed in an ephemeral
Modal Secret by the local entrypoint and is never written into the bundle.
"""

import json
import sys
from pathlib import Path

import modal


SOURCE = Path(__file__).resolve().parents[3] if modal.is_local() else Path("/source")
app = modal.App("spreadsheet-gym-libreoffice-build")
artifacts = modal.Volume.from_name("spreadsheet-gym-runtime-builds", create_if_missing=True)
image = (
    modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
    .apt_install("python3.12", "python3.12-venv", "libreoffice-calc", "python3-uno", "libseccomp2", "ca-certificates")
    .pip_install(
        "fastapi==0.135.2",
        "uvicorn==0.35.0",
        "pydantic==2.12.5",
        "openpyxl==3.1.5",
        "pytest==9.0.3",
        "httpx==0.28.1",
        "pyarrow==22.0.0",
        "numpy==2.2.6",
        "pandas==2.2.3",
        "huggingface_hub==0.36.0",
    )
    .add_local_dir(
        SOURCE / "resources_servers/spreadsheet",
        "/source/resources_servers/spreadsheet",
        ignore=["__pycache__", "*.pyc"],
    )
    .add_local_dir(
        SOURCE / "responses_api_agents/spreadsheet_agent",
        "/source/responses_api_agents/spreadsheet_agent",
        ignore=["__pycache__", "*.pyc"],
    )
    .add_local_file(SOURCE / "LICENSE", "/source/LICENSE")
)


@app.function(
    image=modal.Image.from_registry("ubuntu:24.04", add_python="3.12"),
    volumes={"/artifacts": artifacts},
    cpu=2,
    memory=8192,
    timeout=900,
)
def validate_clean(code_commit: str):
    import os
    import subprocess
    import tarfile
    import tempfile

    with tempfile.TemporaryDirectory(prefix="offline-install-") as temporary:
        archive = Path("/artifacts") / code_commit / "runtime.tar.gz"
        with tarfile.open(archive) as tar:
            tar.extractall(temporary, filter="data")
        bundle = Path(temporary) / "spreadsheet-gym-ubuntu24.04-x86_64"
        installed = subprocess.run(
            ["bash", str(bundle / "install_offline.sh")],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
        if installed.returncode:
            print(installed.stdout[-12000:], flush=True)
            raise RuntimeError("Clean Ubuntu offline installation failed")
        python = str(bundle / "service-venv/bin/python")
        subprocess.run(
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
            check=True,
        )
        tested = subprocess.run(
            [python, "-m", "pytest", "-c", "/dev/null", "-q", "-ra", "resources_servers/spreadsheet/tests"],
            cwd=bundle / "source",
            env=dict(
                os.environ,
                PYTHONPATH=str(bundle / "source"),
                SPREADSHEET_TEST_REAL_LO="1",
                SPREADSHEET_TEST_ROOTFS=str(bundle / "python-root"),
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        print(tested.stdout, flush=True)
        if tested.returncode:
            raise RuntimeError("Clean Ubuntu runtime canary failed")
        return {
            "offline_install_passed": True,
            "test_exit_code": tested.returncode,
            "test_summary": tested.stdout[-5000:],
        }


@app.function(image=image, volumes={"/artifacts": artifacts}, cpu=2, memory=8192, timeout=1800)
def build(code_commit: str, publish_repo: str = ""):
    import hashlib
    import os
    import platform
    import shutil
    import subprocess
    import tarfile
    import time

    sys.path.insert(0, "/source")
    from resources_servers.spreadsheet.packaging.make_python_root import build as make_root

    started = time.monotonic()
    output = Path("/build")
    output.mkdir()
    bundle = output / "spreadsheet-gym-ubuntu24.04-x86_64"
    bundle.mkdir()
    wheelhouse = bundle / "wheelhouse"
    wheelhouse.mkdir()
    requirements = [
        "fastapi==0.135.2",
        "uvicorn==0.35.0",
        "pydantic==2.12.5",
        "openpyxl==3.1.5",
        "pytest==9.0.3",
        "httpx==0.28.1",
        "pyarrow==22.0.0",
        "numpy==2.2.6",
        "pandas==2.2.3",
    ]
    subprocess.run(
        ["python", "-m", "pip", "download", "--only-binary=:all:", "-d", str(wheelhouse), *requirements], check=True
    )
    runtime = bundle / "python-root"
    libraries = make_root(runtime, wheelhouse)
    env = dict(os.environ, PYTHONPATH="/source", SPREADSHEET_TEST_REAL_LO="1", SPREADSHEET_TEST_ROOTFS=str(runtime))
    tests = subprocess.run(
        ["python", "-m", "pytest", "-c", "/dev/null", "-q", "-ra", "/source/resources_servers/spreadsheet/tests"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=600,
    )
    print(tests.stdout, flush=True)
    receipt = {
        "code_commit": code_commit,
        "platform": platform.platform(),
        "glibc": platform.libc_ver(),
        "test_exit_code": tests.returncode,
        "elapsed_seconds": time.monotonic() - started,
        "rootfs_libraries": libraries,
        "libreoffice": subprocess.check_output(["soffice", "--version"], text=True).strip(),
    }
    print("SPREADSHEET_QUALIFICATION_JSON=" + json.dumps(receipt), flush=True)
    if tests.returncode:
        return {"qualified": False, "receipt": receipt}
    if not publish_repo:
        return {"qualified": True, "receipt": receipt, "published": False}
    # Download Ubuntu package closure into the public artifact, never a local Mac cache.
    debs = bundle / "debs"
    (debs / "partial").mkdir(parents=True)
    subprocess.run(["apt-get", "update"], check=True)
    subprocess.run(
        [
            "apt-get",
            "-y",
            "--download-only",
            "--no-install-recommends",
            "-o",
            "Dir::State::status=/dev/null",
            "-o",
            f"Dir::Cache::archives={debs}",
            "install",
            "python3.12",
            "python3.12-venv",
            "libreoffice-calc",
            "python3-uno",
            "libseccomp2",
            "libc-bin",
        ],
        check=True,
    )
    shutil.rmtree(debs / "partial")
    (debs / "lock").unlink(missing_ok=True)
    shutil.copytree("/source", bundle / "source", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    receipt["packages"] = subprocess.check_output(
        ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"], text=True
    ).splitlines()
    (bundle / "qualification.json").write_text(json.dumps(receipt, indent=2))
    shutil.copy2("/source/resources_servers/spreadsheet/packaging/install_offline.sh", bundle / "install_offline.sh")
    (bundle / "install_offline.sh").chmod(0o755)

    def write_checksums():
        checksums = []
        for file in sorted(bundle.rglob("*")):
            if file == bundle / "SHA256SUMS":
                continue
            if file.is_file() and not file.is_symlink():
                with file.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                checksums.append(digest + "  " + str(file.relative_to(bundle)))
        (bundle / "SHA256SUMS").write_text("\n".join(checksums) + "\n")

    write_checksums()
    subprocess.run(["bash", str(bundle / "install_offline.sh")], check=True, timeout=600)
    # Installing the offline .deb closure can update glibc after the first
    # sandbox was created. Align the shipped root with those exact packages.
    aligned = bundle / "python-root-aligned"
    receipt["rootfs_libraries"] = make_root(aligned, wheelhouse)
    shutil.rmtree(runtime)
    aligned.rename(runtime)
    # Verify the installer-created runtime, then omit this non-relocatable venv
    # from the archive. The node creates its own venv at its final path.
    subprocess.run(
        [
            str(bundle / "service-venv/bin/python"),
            "-c",
            "import fastapi,uvicorn,openpyxl; from resources_servers.spreadsheet.service.engines import LibreOfficePool; from pathlib import Path; p=LibreOfficePool(Path('/build/offline-smoke-office')); p.probe(); p.close(); print('offline-venv-and-uno-ready')",
        ],
        cwd=bundle / "source",
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        check=True,
        timeout=90,
    )
    shutil.rmtree(bundle / "service-venv")
    if any((runtime / "workspaces").iterdir()):
        raise RuntimeError("A live sandbox workspace must not enter the public bundle")
    receipt["offline_install_passed"] = True
    (bundle / "qualification.json").write_text(json.dumps(receipt, indent=2))
    write_checksums()
    archive = output / (bundle.name + ".tar.gz")
    with tarfile.open(archive, "w:gz", compresslevel=3) as tar:
        tar.add(bundle, arcname=bundle.name)
    staging = Path("/artifacts") / code_commit
    staging.mkdir(exist_ok=True)
    shutil.copyfile(archive, staging / "runtime.tar.gz")
    artifacts.commit()
    receipt["clean_ubuntu_validation"] = validate_clean.remote(code_commit)
    digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    (output / "SHA256SUMS").write_text(digest + "  " + archive.name + "\n")
    (output / "qualification.json").write_text(json.dumps(receipt, indent=2))
    (output / "README.md").write_text(
        "---\nlicense: apache-2.0\ntags:\n- libreoffice\n- nemo-gym\n- runtime\n---\n\n"
        "# Spreadsheet Gym offline runtime\n\nUbuntu 24.04 x86_64 / glibc 2.39. CPU only. "
        "Includes LibreOffice Ubuntu packages, Python wheels, a minimal Python chroot, and source. "
        "No dataset, oracle, credentials, or company files are included.\n\n"
        "Check SHA256SUMS before unpacking, then read source/resources_servers/spreadsheet/README.md. "
        "Run install_offline.sh as root in the existing service container. "
        "No nested containers or network downloads are required by the installer.\n\n"
        f"Source commit: `{code_commit}`. Qualification: see qualification.json. "
        "Qualification on Modal does not establish compatibility with every target kernel or every dataset task. "
        "Run the target readiness checks and dataset compatibility gate before training.\n\n"
        "Source: Apache-2.0. Dependencies retain their own licenses; wheel metadata and Ubuntu .deb "
        "copyright files are included. Ubuntu source packages and exact versions are available at "
        "https://launchpad.net/ubuntu/+source/libreoffice and https://archive.ubuntu.com/ubuntu/pool/ .\n"
    )
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ["HF_TOKEN"])
    api.create_repo(publish_repo, repo_type="dataset", private=False, exist_ok=True)
    # Explicit allowlist: build staging directories and credentials cannot be uploaded.
    result = api.upload_folder(
        repo_id=publish_repo,
        repo_type="dataset",
        folder_path=str(output),
        allow_patterns=[archive.name, "SHA256SUMS", "qualification.json", "README.md"],
        commit_message=f"Publish qualified spreadsheet runtime {code_commit[:12]}",
    )
    (staging / "runtime.tar.gz").unlink()
    staging.rmdir()
    artifacts.commit()
    return {
        "qualified": True,
        "published": True,
        "repo": publish_repo,
        "archive_sha256": digest,
        "bytes": archive.stat().st_size,
        "commit": result.oid,
        "elapsed_seconds": time.monotonic() - started,
    }


@app.local_entrypoint()
def main(code_commit: str, publish: bool = False, hf_python: str = ""):
    import subprocess

    # The function source is under /source; make it importable before remote execution.
    if publish:
        if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() != code_commit:
            raise RuntimeError("Publication must identify the exact source commit")
        subprocess.run(
            [
                "git",
                "diff",
                "--exit-code",
                "HEAD",
                "--",
                "resources_servers/spreadsheet",
                "responses_api_agents/spreadsheet_agent",
            ],
            cwd=SOURCE,
            check=True,
        )
        # Read only the credential needed for this upload, inside a pre-existing
        # HF-capable Python. Capture it privately; never emit it into logs.
        helper = "from huggingface_hub import HfApi,get_token; import json; t=get_token(); print(json.dumps({'token':t,'owner':HfApi(token=t).whoami()['name']}))"
        credential = json.loads(subprocess.check_output([hf_python or sys.executable, "-c", helper], text=True))
        if not credential["token"]:
            raise RuntimeError("No HF credential available for the explicitly requested public upload")
        repo = credential["owner"] + "/spreadsheet-gym-libreoffice-runtime"
        function = build.with_options(secrets=[modal.Secret.from_dict({"HF_TOKEN": credential["token"]})])
    else:
        function, repo = build, ""
    result = function.remote(code_commit, repo)
    print("SPREADSHEET_BUILD_RESULT=" + json.dumps(result))
