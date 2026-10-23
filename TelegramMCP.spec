# Build with: python -m PyInstaller --noconfirm --clean TelegramMCP.spec
import json
import os
import subprocess
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

source = Path(SPECPATH)
build_info = source / "build" / "windows-provenance"
build_info.mkdir(parents=True, exist_ok=True)
# PyInstaller imports packages while locating native DLLs. Use build-only dummy
# configuration: never load a user's Telegram credentials or authenticated session.
for key in list(os.environ):
    if key.startswith(("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME_")):
        os.environ.pop(key, None)
os.environ["TELEGRAM_API_ID"] = "1"
os.environ["TELEGRAM_API_HASH"] = "build-placeholder-not-a-secret"
os.environ["TELEGRAM_SESSION_NAME"] = str(build_info / "build-only")
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
provenance = build_info / "direct_url.json"
provenance.write_text(json.dumps({
    "url": "https://github.com/AlexFspb/telegram-mcp.git",
    "vcs_info": {"vcs": "git", "commit_id": commit},
}), encoding="utf-8")
metadata = copy_metadata("telegram-mcp")
metadata_destination = metadata[0][1]
data = metadata + [(str(provenance), metadata_destination)]
hidden = ["pystray._win32"]
for package in ("telegram_mcp", "windows_app"):
    for file in (source / package).rglob("*.py"):
        parts = list(file.relative_to(source).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        hidden.append(".".join(parts))

a = Analysis(
    [str(source / "windows_entry.py")], pathex=[str(source)],
    binaries=[], datas=data,
    hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "black", "flake8", "IPython", "jupyter", "faster_whisper"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="TelegramMCP-Setup", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True, hide_console="hide-early",
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
