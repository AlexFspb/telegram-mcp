"""No-window stdio/PE checks. Never enumerate, inspect or control desktop windows."""

import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from windows_app import windows_stdio


def test_pe_header_distinguishes_gui_and_console(tmp_path):
    image = bytearray(256)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 128)
    image[128:132] = b"PE\0\0"
    path = tmp_path / "sample.exe"
    for subsystem in (2, 3):
        struct.pack_into("<H", image, 128 + 24 + 68, subsystem)
        path.write_bytes(image)
        assert windows_stdio.pe_subsystem(path) == subsystem


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI process standard handles")
def test_pythonw_restores_inherited_utf8_pipes_without_a_console():
    interpreter = Path(sys.executable).with_name("pythonw.exe")
    assert interpreter.exists()
    repo = Path(__file__).resolve().parent.parent
    code = (
        "import sys,json; from windows_app.windows_stdio import configure_standard_streams,console_attached; "
        "saved=(sys.stdin,sys.stdout,sys.stderr); sys.stdin=sys.stdout=sys.stderr=None; "
        "configure_standard_streams(require_protocol=True); "
        "data=sys.stdin.readline().strip(); print(json.dumps({'received':data,'console':console_attached()},ensure_ascii=False))"
    )
    result = subprocess.run(
        [str(interpreter), "-c", code],
        input="Привет, Telegram\n".encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=repo,
        timeout=15,
        env=dict(os.environ, PYTHONPATH=str(repo)),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout) == {"received": "Привет, Telegram", "console": False}


@pytest.mark.skipif(os.name != "nt", reason="Windowed executable acceptance")
def test_frozen_executable_is_windows_gui_and_has_no_console():
    executable = os.environ.get("TELEGRAM_TEST_EXE")
    if not executable:
        pytest.skip("Set TELEGRAM_TEST_EXE for the frozen executable acceptance check")
    assert windows_stdio.pe_subsystem(Path(executable)) == 2
    # No CREATE_NO_WINDOW flag: the GUI subsystem itself must prevent a console.
    result = subprocess.run(
        [executable, "--self-check"],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=25,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    check = json.loads(result.stdout)
    assert check["console_attached"] is False
    assert check["pe_subsystem"] == 2
