"""Linux subprocesses only: no Docker, daemon, or privileged container launcher."""

import json
import os
import queue
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Lock

from .errors import EnvironmentError, ToolError


def terminate(process):
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


class LibreOfficePool:
    """Persistent UNO bridge workers; exclusive checkout and worker-local profiles."""

    def __init__(
        self,
        root: Path,
        python: str = "/usr/bin/python3",
        soffice: str = "/usr/bin/soffice",
        workers: int = 2,
        timeout: float = 120,
    ):
        if not 1 <= workers <= 16 or not 1 <= timeout <= 600:
            raise ValueError("Invalid LibreOffice pool size or timeout")
        self.root, self.python, self.soffice, self.timeout = root, python, soffice, timeout
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.slots = queue.Queue()
        self.processes = {}
        self.version = "unqualified"
        for slot in range(workers):
            self.slots.put(slot)
        self._lock = Lock()

    def _read(self, process, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(max(0, deadline - time.monotonic())):
                raise EnvironmentError("LibreOffice worker timed out")
            line = process.stdout.readline(1024 * 1024)
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            raise EnvironmentError("LibreOffice worker failed") from None
        if not message.get("ok"):
            raise EnvironmentError("LibreOffice could not recalculate this workbook")
        return message

    def _get_process(self, slot, deadline):
        process = self.processes.get(slot)
        if process is not None and process.poll() is None:
            return process
        profile = self.root / str(slot)
        profile.mkdir(exist_ok=True, mode=0o700)
        # No service credentials in worker environment or process arguments.
        process = subprocess.Popen(
            [self.python, "-I", "-u", str(Path(__file__).with_name("uno_bridge.py")), str(profile), self.soffice],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(profile), "LANG": "C.UTF-8", "TZ": "UTC"},
        )
        self.processes[slot] = process
        self.version = self._read(process, deadline)["version"]
        return process

    def recalculate(self, path: Path):
        from .ooxml_metadata import capture, restore

        metadata = capture(path)
        try:
            slot = self.slots.get(timeout=self.timeout)
        except queue.Empty:
            raise EnvironmentError("LibreOffice capacity timeout") from None
        process = None
        try:
            deadline = time.monotonic() + self.timeout
            process = self._get_process(slot, deadline)
            process.stdin.write(json.dumps({"path": str(path.resolve())}) + "\n")
            process.stdin.flush()
            self._read(process, deadline)
            return {"metadata_repairs": restore(path, metadata)}
        except Exception as exc:
            terminate(process or self.processes.get(slot))
            self.processes.pop(slot, None)
            if isinstance(exc, EnvironmentError):
                raise
            raise EnvironmentError("LibreOffice worker unavailable") from exc
        finally:
            self.slots.put(slot)

    def probe(self):
        slot = self.slots.get(timeout=self.timeout)
        try:
            self._get_process(slot, time.monotonic() + self.timeout)
        finally:
            self.slots.put(slot)

    def close(self):
        for process in self.processes.values():
            terminate(process)
        self.processes.clear()


class BubblewrapPython:
    def __init__(
        self,
        python: str = sys.executable,
        bwrap: str = "bwrap",
        timeout: int = 60,
        memory_mb: int = 1024,
        output_bytes: int = 65536,
        runtime_paths: list[str] | None = None,
    ):
        self.python, self.bwrap = str(Path(python).absolute()), shutil.which(bwrap)
        self.timeout, self.memory_mb, self.output_bytes = timeout, memory_mb, output_bytes
        self.runtime_paths = [Path(p).resolve() for p in runtime_paths or []]
        if any(p == Path("/") or p == Path("/etc") or p == Path("/tmp") for p in self.runtime_paths):
            raise ValueError("Runtime mounts must not expose the root, secrets, or temporary files")

    def execute(self, workspace: Path, code: str):
        if sys.platform != "linux" or not self.bwrap:
            raise EnvironmentError("Isolated Python requires Linux and working bubblewrap/user namespaces")
        command = [
            self.bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/workspace",
            "--bind",
            str(workspace.resolve()),
            "/workspace",
            "--chdir",
            "/workspace",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "TZ",
            "UTC",
            "--setenv",
            "OPENBLAS_NUM_THREADS",
            "1",
            "--setenv",
            "OMP_NUM_THREADS",
            "1",
        ]
        mounted = []
        for path in [Path(p) for p in ("/usr", "/bin", "/lib", "/lib64")] + self.runtime_paths:
            if path.exists() and not any(path.is_relative_to(parent) for parent in mounted):
                command.extend(["--ro-bind", str(path), str(path)])
                mounted.append(path)
        # -I prevents host PYTHONPATH/user-site injections. Arbitrary user code runs
        # only after the trusted resource limits are set, inside the namespace.
        wrapper = (
            "import resource,runpy;"
            f"resource.setrlimit(resource.RLIMIT_AS,({self.memory_mb * 1024**2},{self.memory_mb * 1024**2}));"
            f"resource.setrlimit(resource.RLIMIT_CPU,({self.timeout},{self.timeout}));"
            "resource.setrlimit(resource.RLIMIT_CORE,(0,0));"
            "resource.setrlimit(resource.RLIMIT_NOFILE,(128,128));"
            "resource.setrlimit(resource.RLIMIT_FSIZE,(104857600,104857600));"
            "runpy.run_path('/workspace/.program.py',run_name='__main__')"
        )
        program = workspace / ".program.py"
        program.write_text(code)
        command += [self.python, "-I", "-c", wrapper]
        try:
            # Drain output incrementally with a hard cap rather than buffering
            # arbitrary model stdout in the service's memory or disk.
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            output = bytearray()
            deadline = time.monotonic() + self.timeout + 5
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        terminate(process)
                        raise ToolError("Python execution timed out; workbook changes were discarded")
                    if not selector.select(remaining):
                        continue
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > self.output_bytes:
                        terminate(process)
                        raise ToolError("Python output limit exceeded; workbook changes were discarded")
            process.wait(timeout=max(1, deadline - time.monotonic()))
            text = output.decode("utf-8", errors="replace")
            if process.returncode:
                if text.startswith("bwrap:"):
                    raise EnvironmentError("Host denied bubblewrap isolation; no unsafe fallback")
                raise ToolError(text[-4000:] or "Python execution failed")
            return {"stdout": text, "exit_code": 0}
        except OSError as exc:
            raise EnvironmentError("Could not launch isolated Python") from exc
        finally:
            program.unlink(missing_ok=True)
            if "process" in locals() and process.poll() is None:
                terminate(process)

    def probe(self):
        with tempfile.TemporaryDirectory(prefix="spreadsheet-isolation-") as directory:
            self.execute(Path(directory), "print('isolated-python-ready')")
