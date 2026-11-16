"""Trusted child bootstrap. Invoke only from ChrootPython, never via HTTP.

Needs CAP_SYS_CHROOT/CAP_SETUID/CAP_SETGID, not CAP_SYS_ADMIN or namespaces.
The child permanently drops privilege and installs seccomp before executing code.
"""

import ctypes
import errno
import os
import resource
import sys


def main():
    root, uid, job_name, seconds, memory_mb = sys.argv[1:]
    uid, seconds, memory_mb = int(uid), int(seconds), int(memory_mb)
    if os.geteuid() != 0 or uid < 10000:
        raise RuntimeError("Privileged bootstrap requires root and an unprivileged job UID")
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot initialize seccomp")
    try:
        # Network, cross-process introspection, namespace/identity changes and
        # process-group escapes are not required for spreadsheet Python.
        denied = (
            "socket",
            "socketpair",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "mount",
            "umount2",
            "unshare",
            "setns",
            "open_by_handle_at",
            "bpf",
            "keyctl",
            "add_key",
            "request_key",
            "perf_event_open",
            "reboot",
            "kexec_load",
            "userfaultfd",
            "io_uring_setup",
            "setsid",
            "setpgid",
            "chroot",
            "setuid",
            "setreuid",
            "setresuid",
            "setfsuid",
            "setgid",
            "setregid",
            "setresgid",
            "setfsgid",
            "setgroups",
        )
        for name in denied:
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise RuntimeError("Cannot install a required seccomp rule")
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise RuntimeError("Cannot set no_new_privs")
        os.chroot(root)
        os.chdir("/workspaces/" + job_name)
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
        if os.geteuid() != uid or os.getegid() != uid:
            raise RuntimeError("Privilege drop failed")
        for kind, value in (
            (resource.RLIMIT_CORE, 0),
            (resource.RLIMIT_AS, memory_mb * 1024**2),
            (resource.RLIMIT_CPU, seconds),
            (resource.RLIMIT_FSIZE, 100 * 1024**2),
            (resource.RLIMIT_NOFILE, 128),
            (resource.RLIMIT_NPROC, 32),
        ):
            resource.setrlimit(kind, (value, value))
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Host rejected seccomp")
        os.execve(
            "/usr/bin/python3.12",
            ["/usr/bin/python3.12", "-I", ".program.py"],
            {
                "HOME": "/workspaces/" + job_name + "/.home",
                "TMPDIR": "/workspaces/" + job_name + "/.tmp",
                "PATH": "/usr/bin",
                "LANG": "C.UTF-8",
                "TZ": "UTC",
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            },
        )
    finally:
        seccomp.seccomp_release(context)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Keep host paths and the supervisor's environment out of observations.
        print("SPREADSHEET_ISOLATION_SETUP_FAILED", file=sys.stderr, flush=True)
        sys.exit(125)
