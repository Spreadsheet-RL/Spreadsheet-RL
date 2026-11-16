import os
import queue
import selectors
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from .engines import terminate
from .errors import EnvironmentError, ToolError


class ChrootPython:
    """One isolated UID per active job in a minimal root-owned Python filesystem."""

    def __init__(
        self,
        root: Path,
        workers: int = 8,
        base_uid: int = 60000,
        timeout: int = 60,
        memory_mb: int = 1024,
        output_bytes: int = 65536,
    ):
        self.root = root.resolve()
        self.timeout, self.memory_mb, self.output_bytes = timeout, memory_mb, output_bytes
        if base_uid < 10000 or not 1 <= workers <= 64:
            raise ValueError("Invalid sandbox UID pool")
        self.uids = queue.Queue()
        for uid in range(base_uid, base_uid + workers):
            self.uids.put(uid)

    def execute(self, workspace: Path, code: str):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise EnvironmentError("chroot_uid_seccomp needs Linux root with chroot/setuid/setgid capabilities")
        if self.root == Path("/") or not (self.root / "usr/bin/python3.12").is_file():
            raise EnvironmentError("A qualified minimal Python root filesystem is required")
        # A model must not be able to replace runtime files or the jail root.
        for parent in (self.root, self.root / "usr", self.root / "usr/bin", self.root / "usr/bin/python3.12"):
            metadata = parent.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise EnvironmentError("Sandbox runtime must be root-owned and not group/world writable")
        try:
            uid = self.uids.get(timeout=self.timeout)
        except queue.Empty:
            raise EnvironmentError("Python execution capacity timeout") from None
        jails = self.root / "workspaces"
        jails.mkdir(exist_ok=True, mode=0o711)
        name = uuid4().hex
        jail = jails / name
        process = None
        try:
            jail.mkdir(mode=0o700)
            if (workspace / "data.xlsx").exists():
                shutil.copyfile(workspace / "data.xlsx", jail / "data.xlsx")
            (jail / ".program.py").write_text(code)
            (jail / ".tmp").mkdir(mode=0o700)
            (jail / ".home").mkdir(mode=0o700)
            for path in (jail, *jail.iterdir()):
                os.chown(path, uid, uid)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("chroot_bootstrap.py")),
                    str(self.root),
                    str(uid),
                    name,
                    str(self.timeout),
                    str(self.memory_mb),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
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
                        raise ToolError("Python execution timed out; changes discarded")
                    if not selector.select(remaining):
                        continue
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > self.output_bytes:
                        raise ToolError("Python output limit exceeded; changes discarded")
            process.wait(timeout=max(1, deadline - time.monotonic()))
            text = output.decode(errors="replace")
            # Kill descendants even when their parent exited successfully. The
            # seccomp policy forbids escaping this process group with setsid/setpgid.
            terminate(process)
            if process.returncode == 125:
                raise EnvironmentError("Host rejected chroot/UID/seccomp setup; no unsafe fallback")
            if process.returncode:
                raise ToolError(text[-4000:] or "Python execution failed")
            candidate = jail / "data.xlsx"
            if candidate.exists() or candidate.is_symlink():
                descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 100 * 1024**2:
                        raise ToolError("Python produced an invalid workbook file")
                    with (workspace / "data.xlsx").open("wb") as destination:
                        shutil.copyfileobj(stream, destination)
            elif (workspace / "data.xlsx").exists():
                raise ToolError("Python removed data.xlsx; changes discarded")
            return {"stdout": text, "exit_code": 0}
        except OSError as exc:
            raise EnvironmentError("Python isolation or artifact transfer failed") from exc
        finally:
            if process is not None:
                terminate(process)
            shutil.rmtree(jail, ignore_errors=True)
            self.uids.put(uid)

    def probe(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="spreadsheet-chroot-probe-") as temporary:
            # Verify both privilege drop and blocked network access, not just exit 0.
            code = (
                "import os,socket\n"
                "assert os.geteuid() >= 10000\n"
                "assert not os.path.exists('/proc/self/root')\n"
                "try:\n socket.socket()\n"
                "except PermissionError:\n pass\n"
                "else:\n raise RuntimeError('network is not blocked')\n"
                "print('chroot-uid-seccomp-ready')\n"
            )
            result = self.execute(Path(temporary), code)
            if "chroot-uid-seccomp-ready" not in result["stdout"]:
                raise EnvironmentError("Isolation probe failed")
