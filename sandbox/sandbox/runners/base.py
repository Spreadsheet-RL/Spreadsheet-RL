# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional

import psutil
import structlog
import platform
import resource

from sandbox.configs.run_config import RunConfig
from sandbox.runners.isolation import tmp_cgroup, tmp_netns, tmp_overlayfs
from sandbox.runners.types import CodeRunArgs, CodeRunResult, CommandRunResult, CommandRunStatus
from sandbox.utils.common import set_permissions_recursively
from sandbox.utils.execution import cleanup_process, ensure_bash_integrity, get_output_non_blocking, kill_process_tree

logger = structlog.stdlib.get_logger()
config = RunConfig.get_instance_sync()

_DEFAULT_DATA_ROOT = "/opt/sandboxfusion/data"
_DEFAULT_WORKSPACE_DIRNAME = "_workspaces"
_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WORKSPACE_MANIFEST_VERSION = 1


def _sanitize_workspace_id(workspace_id: Optional[str]) -> Optional[str]:
    """Validate and normalize a workspace id.

    The workspace id becomes a directory name under the workspace root, so it is
    intentionally restricted to a small character set.
    """
    if not workspace_id or not isinstance(workspace_id, str):
        return None
    workspace_id = workspace_id.strip()
    if not _WORKSPACE_ID_RE.match(workspace_id):
        return None
    return workspace_id


def _sanitize_relpath(relpath: Optional[str]) -> Optional[str]:
    if not relpath or not isinstance(relpath, str) or "\0" in relpath:
        return None
    relpath = relpath.strip()
    if not relpath or os.path.isabs(relpath):
        return None
    normalized = os.path.normpath(relpath)
    parts = [p for p in normalized.split(os.sep) if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return os.path.join(*parts)


def _normalize_workspace_paths(workspace_paths: List[str]) -> List[str]:
    normalized: set[str] = set()
    for rel in workspace_paths:
        rel_norm = _sanitize_relpath(rel)
        if not rel_norm:
            continue
        if rel_norm == _DEFAULT_WORKSPACE_DIRNAME or rel_norm.startswith(_DEFAULT_WORKSPACE_DIRNAME + os.sep):
            continue
        normalized.add(rel_norm)
    return sorted(normalized)


def _is_flat_output_xlsx_request(workspace_paths: List[str]) -> bool:
    requested = _normalize_workspace_paths(workspace_paths)
    if len(requested) != 1:
        return False
    rel_norm = requested[0]
    rel_norm_lower = rel_norm.lower()
    return rel_norm_lower == "output.xlsx" or rel_norm_lower.endswith(os.sep + "output.xlsx")


def _atomic_copy_file(src: Path, dest: Path) -> None:
    tmp_dest = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}")
    try:
        if tmp_dest.is_symlink() or tmp_dest.is_file():
            tmp_dest.unlink()
        elif tmp_dest.is_dir():
            shutil.rmtree(tmp_dest)

        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)

        shutil.copy2(src, tmp_dest)
        os.replace(tmp_dest, dest)
    finally:
        try:
            if tmp_dest.is_symlink() or tmp_dest.is_file():
                tmp_dest.unlink()
            elif tmp_dest.is_dir():
                shutil.rmtree(tmp_dest)
        except OSError:
            pass


def _sync_workspace_tree(src_root: Path, dst_root: Path) -> None:
    dst_root_real = dst_root.resolve()
    for root, dirs, files in os.walk(src_root, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(src_root)
        dst_path = dst_root / rel_root
        try:
            dst_path.resolve().relative_to(dst_root_real)
        except Exception as e:
            logger.warning(
                "workspace sync skipped for escaped destination",
                dst_path=str(dst_path),
                error=str(e),
            )
            continue

        if dst_path.is_symlink():
            dst_path.unlink()
        if dst_path.exists() and not dst_path.is_dir():
            if dst_path.is_symlink() or dst_path.is_file():
                dst_path.unlink()
            else:
                shutil.rmtree(dst_path)
        dst_path.mkdir(parents=True, exist_ok=True)

        dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]

        for filename in files:
            src = root_path / filename
            if src.is_symlink() or not src.is_file():
                continue
            dest = dst_path / filename
            try:
                dest.resolve().relative_to(dst_root_real)
            except Exception as e:
                logger.warning(
                    "workspace sync skipped for escaped file destination",
                    dest=str(dest),
                    error=str(e),
                )
                continue
            _atomic_copy_file(src, dest)


def _ensure_workspace_writable(workspace_dir: str) -> None:
    sandbox_config = getattr(config, "sandbox", None)
    set_uid = getattr(sandbox_config, "set_uid", None) if sandbox_config else None
    if not set_uid:
        return

    try:
        os.chmod(workspace_dir, 0o777)
    except OSError as e:
        logger.warning("failed to chmod workspace directory", workspace_dir=workspace_dir, error=str(e))

    for root, dirs, files in os.walk(workspace_dir, followlinks=False):
        for dir_name in dirs:
            path = os.path.join(root, dir_name)
            try:
                if os.path.islink(path):
                    continue
                os.chmod(path, 0o777)
            except OSError as e:
                logger.warning("failed to chmod workspace directory entry", path=path, error=str(e))

        for file_name in files:
            path = os.path.join(root, file_name)
            try:
                if os.path.islink(path):
                    continue
                os.chmod(path, 0o666)
            except OSError as e:
                logger.warning("failed to chmod workspace file entry", path=path, error=str(e))


def _copy_workspace_paths(workspace_dir: str, data_root: str, workspace_paths: List[str]) -> None:
    """Copy requested dataset paths from `data_root` into `workspace_dir`.

    The caller provides `workspace_paths` as normalized dataset-relative paths
    (see `_normalize_workspace_paths`). This helper:
      - Ensures `src` resolves under `data_root` (defends against symlink escape).
      - Skips symlinks (both as the top-level source and within copied trees).

    Raises:
        RuntimeError: When a requested copy fails (e.g., permission/disk errors).
    """
    if not workspace_paths:
        return

    data_root_real = os.path.realpath(data_root)

    def _ignore_symlinks_in_tree(directory: str, entries: List[str]) -> List[str]:
        ignored: List[str] = []
        for entry in entries:
            entry_path = os.path.join(directory, entry)
            try:
                if os.path.islink(entry_path):
                    ignored.append(entry)
            except OSError as e:
                logger.warning(
                    "Error checking if path is a symlink; ignoring in workspace copy",
                    path=entry_path,
                    error=str(e),
                )
                ignored.append(entry)
        return ignored

    if len(workspace_paths) == 1:
        rel_norm = workspace_paths[0]
        if rel_norm.lower() == "output.xlsx" or rel_norm.lower().endswith(os.sep + "output.xlsx"):
            src = os.path.join(data_root, rel_norm)
            src_real = os.path.realpath(src)
            try:
                Path(src_real).relative_to(data_root_real)
            except ValueError:
                src = ""
            if src and os.path.isfile(src) and not os.path.islink(src):
                try:
                    output_path = Path(workspace_dir) / "output.xlsx"
                    try:
                        if output_path.is_symlink() or output_path.is_file():
                            output_path.unlink()
                        elif output_path.is_dir():
                            shutil.rmtree(output_path)
                    except OSError as e:
                        logger.warning(
                            "failed to remove existing output.xlsx in workspace seed",
                            workspace_dir=workspace_dir,
                            output_path=str(output_path),
                            error=str(e),
                        )
                    data_path = Path(workspace_dir) / "data.xlsx"
                    _atomic_copy_file(Path(src), data_path)
                    return
                except OSError as e:
                    raise RuntimeError(
                        f"Failed to copy file '{rel_norm}' from '{src}' to '{workspace_dir}'"
                    ) from e

    for rel_norm in workspace_paths:
        src = os.path.join(data_root, rel_norm)
        src_real = os.path.realpath(src)
        try:
            Path(src_real).relative_to(data_root_real)
        except ValueError:
            logger.warning(
                "Workspace path skipped because source is outside data root",
                rel_path=rel_norm,
                src=src,
                src_real=src_real,
                data_root=data_root_real,
            )
            continue

        if os.path.islink(src):
            logger.warning("Workspace path skipped because source is a symlink", rel_path=rel_norm, src=src)
            continue

        dst = os.path.join(workspace_dir, rel_norm)
        src_is_dir = os.path.isdir(src)
        src_is_file = os.path.isfile(src)
        if src_is_dir or src_is_file:
            try:
                parent_dir = os.path.dirname(dst)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                if os.path.lexists(dst) and os.path.islink(dst):
                    os.unlink(dst)
                elif src_is_dir and os.path.lexists(dst) and os.path.isfile(dst):
                    os.remove(dst)
                elif src_is_file and os.path.lexists(dst) and os.path.isdir(dst):
                    shutil.rmtree(dst)

                if src_is_dir:
                    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore_symlinks_in_tree)
                else:
                    shutil.copy2(src, dst)
            except OSError as e:
                item_type = "directory" if src_is_dir else "file"
                raise RuntimeError(f"Failed to copy {item_type} '{rel_norm}' from '{src}' to '{dst}'") from e
        else:
            if not os.path.exists(src):
                logger.warning("Workspace path skipped because source does not exist", rel_path=rel_norm, src=src, dst=dst)
            else:
                logger.warning(
                    "Workspace path skipped because source is not a regular file or directory",
                    rel_path=rel_norm,
                    src=src,
                    dst=dst,
                )


def _prepare_workspace_data_root(workspace_id: str, workspace_paths: List[str]) -> Optional[str]:
    """Prepare a per-workspace dataset view under the bind-mounted data root.

    Returns:
        The workspace directory path to be used as the effective data root, or None.
    """
    workspace_id = _sanitize_workspace_id(workspace_id)
    if not workspace_id:
        return None

    data_root = os.environ.get("SANDBOX_DATA_ROOT", _DEFAULT_DATA_ROOT)
    if not data_root or not os.path.isdir(data_root):
        return None

    workspace_root = os.environ.get("SANDBOX_WORKSPACE_ROOT")
    if workspace_root is not None:
        workspace_root = workspace_root.strip()
    workspace_root = workspace_root or os.path.join(data_root, _DEFAULT_WORKSPACE_DIRNAME)

    os.makedirs(workspace_root, exist_ok=True)
    locks_dir = os.path.join(workspace_root, ".locks")
    os.makedirs(locks_dir, exist_ok=True)
    workspace_dir = os.path.join(workspace_root, workspace_id)
    os.makedirs(workspace_dir, exist_ok=True)

    # File lock to avoid copy races across concurrent requests.
    lock_f = None
    try:
        try:
            import fcntl  # POSIX-only; ok for Linux/Apptainer deployments.
        except ImportError as e:
            logger.error(
                "fcntl module not available; cannot acquire workspace copy lock",
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                error=str(e),
            )
            raise RuntimeError("Workspace locking unavailable; cannot safely prepare workspace") from e

        lock_path = os.path.join(locks_dir, f"{workspace_id}.lock")
        try:
            lock_f = open(lock_path, "a+")
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            logger.error(
                "failed to acquire workspace copy lock; aborting due to risk of race condition",
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                lock_path=lock_path,
                error=str(e),
            )
            raise RuntimeError(f"Failed to acquire workspace lock for '{workspace_id}'") from e

        manifest_path = os.path.join(locks_dir, f"{workspace_id}.manifest.json")
        requested_paths = _normalize_workspace_paths(workspace_paths)
        if not requested_paths:
            raise ValueError("workspace_paths contained no valid dataset-relative paths")

        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                manifest_paths = manifest.get("workspace_paths")
                manifest_version = manifest.get("version")
                if (
                    manifest_version != _WORKSPACE_MANIFEST_VERSION
                    or not isinstance(manifest_paths, list)
                    or any(not isinstance(p, str) for p in manifest_paths)
                ):
                    raise ValueError("invalid manifest schema")
                manifest_paths = sorted(manifest_paths)
                if manifest_paths != requested_paths:
                    raise RuntimeError(
                        f"workspace_id '{workspace_id}' already prepared with different workspace_paths"
                    )
                return workspace_dir
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "failed to read workspace manifest; will re-copy",
                    workspace_id=workspace_id,
                    manifest_path=manifest_path,
                    error=str(e),
                )

        _copy_workspace_paths(workspace_dir, data_root, requested_paths)

        tmp_manifest_path = f"{manifest_path}.tmp.{os.getpid()}"
        try:
            with open(tmp_manifest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": _WORKSPACE_MANIFEST_VERSION,
                        "workspace_paths": requested_paths,
                    },
                    f,
                    sort_keys=True,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_manifest_path, manifest_path)
        finally:
            try:
                if os.path.exists(tmp_manifest_path):
                    os.remove(tmp_manifest_path)
            except OSError as e:
                logger.warning(
                    "failed to remove temporary manifest file",
                    path=tmp_manifest_path,
                    error=str(e),
                )
    finally:
        try:
            if lock_f is not None:
                lock_f.close()
        except OSError as e:
            logger.warning(
                "failed to close workspace lock file",
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                error=str(e),
            )
        _ensure_workspace_writable(workspace_dir)
    return workspace_dir


def _expose_bind_mounted_data_dir(cwd: str, *, data_root_override: Optional[str] = None) -> None:
    """
    SandboxFusion executes user code in a fresh temp directory (cwd).

    For Spreadsheet-RL workloads, the dataset is bind-mounted at
    `/opt/sandboxfusion/data`, while user code often references files via
    relative paths like `data/...`. This helper makes `data/...` resolve by
    creating `cwd/data/*` symlinks pointing at the bind-mounted dataset root.
    """
    if not cwd or not os.path.isdir(cwd):
        return

    data_root = data_root_override or os.environ.get("SANDBOX_DATA_ROOT", _DEFAULT_DATA_ROOT)
    if not data_root or not os.path.isdir(data_root):
        return

    local_data_dir = os.path.join(cwd, "data")
    if os.path.lexists(local_data_dir) and not os.path.isdir(local_data_dir):
        logger.warning(
            "local data dir exists but is not a directory; skipping data exposure",
            path=local_data_dir,
        )
        return

    if os.path.islink(local_data_dir):
        try:
            os.unlink(local_data_dir)
        except OSError as e:
            logger.warning(
                "failed to remove existing local data dir symlink; skipping data exposure",
                path=local_data_dir,
                error=str(e),
            )
            return

    os.makedirs(local_data_dir, exist_ok=True)

    try:
        entries = os.listdir(data_root)
    except OSError as e:
        logger.warning(
            "failed to list bind-mounted data dir for exposure",
            data_root=data_root,
            error=str(e),
        )
        return

    for name in entries:
        if not name or name in {".", ".."} or name.startswith("."):
            continue
        src = os.path.join(data_root, name)
        dst = os.path.join(local_data_dir, name)
        if os.path.lexists(dst):
            continue
        try:
            os.symlink(src, dst)
        except FileExistsError:
            continue
        except OSError as e:
            logger.warning(
                "failed to expose bind-mounted data dir entry",
                data_root=data_root,
                name=name,
                error=str(e),
            )
            continue


async def run_command_bare(command: str | List[str],
                           timeout: float = 10,
                           stdin: Optional[str] = None,
                           cwd: Optional[str] = None,
                           extra_env: Optional[Dict[str, str]] = {},
                           use_exec: bool = False,
                           preexec_fn=None) -> CommandRunResult:
    try:
        logger.debug(f'running command {command}')
        if use_exec:
            p = await asyncio.create_subprocess_exec(*command,
                                                     stdin=subprocess.PIPE,
                                                     stdout=subprocess.PIPE,
                                                     stderr=subprocess.PIPE,
                                                     env={
                                                         **os.environ,
                                                         **(extra_env or {})
                                                     },
                                                     preexec_fn=preexec_fn)
        else:
            p = await asyncio.create_subprocess_shell(command,
                                                      stdin=subprocess.PIPE,
                                                      stdout=subprocess.PIPE,
                                                      stderr=subprocess.PIPE,
                                                      cwd=cwd,
                                                      executable='/bin/bash',
                                                      env={
                                                          **os.environ,
                                                          **(extra_env or {})
                                                      },
                                                      preexec_fn=preexec_fn)
        if stdin is not None:
            try:
                if p.stdin:
                    p.stdin.write(stdin.encode())
                    p.stdin.flush()
                else:
                    logger.warning("Attempted to write to stdin, but stdin is closed.")
            except Exception as e:
                logger.exception(f"Failed to write to stdin: {e}")
        if p.stdin:
            try:
                p.stdin.close()
            except Exception as e:
                logger.warning(f"Failed to close stdin: {e}")
        start_time = time.time()
        try:
            await asyncio.wait_for(p.wait(), timeout=timeout)
            execution_time = time.time() - start_time
            logger.debug(f'stop running command {command}')
        except asyncio.TimeoutError:
            return CommandRunResult(status=CommandRunStatus.TimeLimitExceeded,
                                    execution_time=time.time() - start_time,
                                    stdout=await get_output_non_blocking(p.stdout),
                                    stderr=await get_output_non_blocking(p.stderr))
        finally:
            if psutil.pid_exists(p.pid):
                kill_process_tree(p.pid)
                logger.info(f'process killed: {p.pid}')
            if config.sandbox.cleanup_process:
                cleanup_process()
            if config.sandbox.restore_bash:
                ensure_bash_integrity()

        return CommandRunResult(status=CommandRunStatus.Finished,
                                execution_time=execution_time,
                                return_code=p.returncode,
                                stdout=await get_output_non_blocking(p.stdout),
                                stderr=await get_output_non_blocking(p.stderr))
    except Exception as e:
        message = f'exception on running command {command}: {e} | {traceback.print_tb(e.__traceback__)}'
        logger.warning(message)
        return CommandRunResult(status=CommandRunStatus.Error, stderr=message)


async def run_commands(compile_command: Optional[str], run_command: str, cwd: str, extra_env: Optional[Dict[str, str]],
                       args: CodeRunArgs, **kwargs) -> CodeRunResult:
    files = {}
    compile_res = None
    run_res = None

    workspace_data_root = None
    workspace_id_present = bool(args.workspace_id)
    workspace_paths_present = bool(args.workspace_paths)
    if workspace_id_present != workspace_paths_present:
        raise ValueError("workspace_id and workspace_paths must both be provided or both be omitted")

    use_flat_workbook_view = False
    if workspace_id_present:
        try:
            workspace_data_root = await asyncio.to_thread(
                _prepare_workspace_data_root, args.workspace_id, args.workspace_paths
            )
        except Exception as e:
            logger.warning("failed to prepare workspace", error=str(e), workspace_id=args.workspace_id)
            raise
        if not workspace_data_root:
            raise RuntimeError("workspace requested but could not be prepared")
        use_flat_workbook_view = _is_flat_output_xlsx_request(args.workspace_paths)

    use_workspace_cwd = bool(workspace_data_root and use_flat_workbook_view and args.language == "python")
    exec_cwd = workspace_data_root if use_workspace_cwd else cwd

    if config.sandbox.isolation == 'none':
        preexec_steps = []
        if kwargs.get('set_uid'):
            if not use_workspace_cwd:
                os.makedirs(os.path.join(cwd, "data"), exist_ok=True)
            set_permissions_recursively(cwd, 0o777)
            preexec_steps.append(lambda: os.setuid(kwargs.get('set_uid')))

        if not use_workspace_cwd:
            _expose_bind_mounted_data_dir(cwd, data_root_override=workspace_data_root)
        
        # Apply memory limit using resource module
        if args.memory_limit_MB > 0:
            def memory_limit_preexec():
                _, hard_memory_limit_AS = resource.getrlimit(resource.RLIMIT_AS)
                _, hard_memory_limit_DATA = resource.getrlimit(resource.RLIMIT_DATA)
                soft_memory_limit = args.memory_limit_MB * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (soft_memory_limit, hard_memory_limit_AS))
                resource.setrlimit(resource.RLIMIT_DATA, (soft_memory_limit, hard_memory_limit_DATA))
                if platform.uname().system != "Darwin":
                    _, hard_memory_limit_STACK = resource.getrlimit(resource.RLIMIT_STACK)
                    resource.setrlimit(resource.RLIMIT_STACK, (soft_memory_limit, hard_memory_limit_STACK))
            preexec_steps.insert(0, memory_limit_preexec)
        preexec_fn = lambda: [step() for step in preexec_steps] if preexec_steps else None
        
        if compile_command is not None:
            compile_res = await run_command_bare(compile_command,
                                                 args.compile_timeout,
                                                 None,
                                                 exec_cwd,
                                                 extra_env,
                                                 preexec_fn=preexec_fn)
        if compile_res is None or (compile_res.status == CommandRunStatus.Finished and compile_res.return_code == 0):
            run_res = await run_command_bare(run_command,
                                             args.run_timeout,
                                             args.stdin,
                                             exec_cwd,
                                             extra_env,
                                             preexec_fn=preexec_fn)
        for filename in args.fetch_files:
            fp = os.path.abspath(os.path.join(exec_cwd, filename))
            if os.path.isfile(fp):
                with open(fp, 'rb') as f:
                    content = f.read()
                base64_content = base64.b64encode(content).decode('utf-8')
                files[filename] = base64_content
        return CodeRunResult(compile_result=compile_res, run_result=run_res, files=files)

    elif config.sandbox.isolation == 'lite':
        if not use_workspace_cwd:
            _expose_bind_mounted_data_dir(cwd, data_root_override=workspace_data_root)
        async with tmp_overlayfs() as root, tmp_cgroup(mem_limit='4G', cpu_limit=1) as cgroups, tmp_netns(
                kwargs.get('netns_no_bridge', False)) as netns:
            prefix = []
            for cg in cgroups:
                prefix += ['cgexec', '-g', cg]
            if not kwargs.get('disable_pid_isolation', False):
                prefix += ['unshare', '--pid', '--fork', '--mount-proc']
            prefix += ['ip', 'netns', 'exec', netns]
            prefix += ['chroot', root]

            if compile_command is not None:
                compile_res = await run_command_bare(prefix + ['bash', '-c', f'cd {exec_cwd} && {compile_command}'],
                                                     args.compile_timeout, None, cwd, extra_env, True)
            if compile_res is None or (compile_res.status == CommandRunStatus.Finished and
                                       compile_res.return_code == 0):
                run_res = await run_command_bare(prefix + ['bash', '-c', f'cd {exec_cwd} && {run_command}'],
                                                 args.run_timeout, args.stdin, cwd, extra_env, True)

            if use_workspace_cwd and workspace_data_root:
                overlay_workspace_dir = Path(root) / os.path.abspath(workspace_data_root)[1:]
                if overlay_workspace_dir.is_dir():
                    await asyncio.to_thread(_ensure_workspace_writable, workspace_data_root)
                    await asyncio.to_thread(_sync_workspace_tree, overlay_workspace_dir, Path(workspace_data_root))
                else:
                    logger.warning(
                        "workspace overlay dir missing; skipping sync",
                        overlay_dir=str(overlay_workspace_dir),
                        workspace_dir=workspace_data_root,
                    )

            for filename in args.fetch_files:
                fp = os.path.join(root, os.path.abspath(os.path.join(exec_cwd, filename))[1:])
                if os.path.isfile(fp):
                    with open(fp, 'rb') as f:
                        content = f.read()
                    base64_content = base64.b64encode(content).decode('utf-8')
                    files[filename] = base64_content
            return CodeRunResult(compile_result=compile_res, run_result=run_res, files=files)


def restore_files(dir: str, files: Dict[str, Optional[str]]):
    for filename, content in files.items():
        if not isinstance(content, str):
            continue
        if "IGNORE_THIS_FILE" in filename:
            continue
        filepath = os.path.join(dir, filename)
        dirpath = os.path.dirname(filepath)
        os.makedirs(dirpath, exist_ok=True)
        with open(filepath, 'wb') as file:
            file.write(base64.b64decode(content))
