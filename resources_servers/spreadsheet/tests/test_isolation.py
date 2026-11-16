import os
from pathlib import Path

import pytest

from resources_servers.spreadsheet.service.chroot_runner import ChrootPython
from resources_servers.spreadsheet.service.errors import EnvironmentError, ToolError


def test_chroot_has_no_unsafe_non_linux_fallback(tmp_path):
    if os.geteuid() == 0 and os.uname().sysname == "Linux":
        pytest.skip("Non-root rejection is covered on the Mac")
    runner = ChrootPython(tmp_path)
    with pytest.raises(EnvironmentError):
        runner.execute(tmp_path, "print('must not execute')")


@pytest.mark.skipif(not os.environ.get("SPREADSHEET_TEST_ROOTFS"), reason="Requires qualified Linux Python rootfs")
def test_real_chroot_seccomp_no_oracle_no_network_and_no_privilege(tmp_path):
    root = Path(os.environ["SPREADSHEET_TEST_ROOTFS"])
    runner = ChrootPython(root, timeout=10)
    private = tmp_path / "oracle-secret.txt"
    private.write_text("not-for-model")
    code = f"""import os,socket
assert os.geteuid() >= 10000
assert not os.path.exists({str(private)!r})
assert not os.path.exists('/proc/self/root')
assert 'HF_TOKEN' not in os.environ
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError('network access was allowed')
try:
    os.setuid(0)
except PermissionError:
    pass
else:
    raise AssertionError('privilege escalation was allowed')
from openpyxl import Workbook
wb = Workbook()
wb.active['A1'] = 42
wb.save('data.xlsx')
print('isolated-workbook-created')
"""
    result = runner.execute(tmp_path, code)
    assert "isolated-workbook-created" in result["stdout"]
    assert (tmp_path / "data.xlsx").exists()
    assert private.read_text() == "not-for-model"


@pytest.mark.skipif(not os.environ.get("SPREADSHEET_TEST_ROOTFS"), reason="Requires qualified Linux Python rootfs")
def test_real_chroot_timeout_and_stdout_cap(tmp_path):
    runner = ChrootPython(Path(os.environ["SPREADSHEET_TEST_ROOTFS"]), timeout=1, output_bytes=1024)
    with pytest.raises(ToolError):
        runner.execute(tmp_path, "while True: pass")
    with pytest.raises(ToolError, match="output limit"):
        runner.execute(tmp_path, "print('x'*100000)")
