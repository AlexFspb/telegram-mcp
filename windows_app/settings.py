"""Private state and non-destructive MCP client registration."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib

from dotenv import dotenv_values

TASK_NAME = "AlexTelegramMCP"
CREATE_HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def root() -> Path:
    return Path(
        os.environ.get("ALEX_TELEGRAM_MCP_HOME")
        or Path(os.environ["LOCALAPPDATA"]) / "AlexTelegramMCP"
    )


def state() -> Path:
    return root() / "state"


def env_path() -> Path:
    return state() / ".env"


def executable() -> Path:
    return root() / "TelegramMCP.exe"


def command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(executable()), *args]
    return [
        sys.executable,
        str(Path(__file__).resolve().parent.parent / "windows_entry.py"),
        *args,
    ]


def run_hidden(args, **kwargs):
    return subprocess.run(
        args, creationflags=CREATE_HIDDEN, capture_output=True, text=True, **kwargs
    )


def user_sid() -> str:
    result = run_hidden(["whoami", "/user", "/fo", "csv", "/nh"], check=True)
    return next(csv.reader([result.stdout.strip()]))[1]


def prepare() -> None:
    root().mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        run_hidden(
            [
                "icacls",
                str(root()),
                "/inheritance:r",
                "/grant:r",
                f"*{user_sid()}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
            ],
            check=True,
        )
    state().mkdir(exist_ok=True)
    (root() / "files").mkdir(exist_ok=True)
    (root() / "logs").mkdir(exist_ok=True)


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".telegram-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def values() -> dict:
    return dict(dotenv_values(env_path()))


def save(values_to_save: dict) -> None:
    current = values()
    current.update({key: str(value) for key, value in values_to_save.items()})
    lines = []
    for key, value in current.items():
        if value is None:
            continue
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'")
    atomic_write(env_path(), "\n".join(lines) + "\n")


def save_credentials(api_id: str, api_hash: str) -> None:
    if (
        not api_id.isdecimal()
        or int(api_id) <= 0
        or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash)
    ):
        raise ValueError("Проверьте API ID и API hash на my.telegram.org/apps.")
    prepare()
    save(
        {
            "TELEGRAM_API_ID": api_id,
            "TELEGRAM_API_HASH": api_hash,
            "TELEGRAM_SESSION_NAME": (state() / "telegram").as_posix(),
            "TELEGRAM_DEVICE_MODEL": "Alex Telegram MCP",
            "TELEGRAM_SYSTEM_VERSION": "Windows",
            "TELEGRAM_APP_VERSION": "1.0.0",
            "TELEGRAM_SERVER_ROOTS_ONLY": "1",
            "TELEGRAM_EXPOSED_TOOLS": "all",
            "TELEGRAM_ALIASES_FILE": (state() / "aliases.json").as_posix(),
            "TELEGRAM_TRANSCRIPT_CACHE_DIR": (state() / "transcripts").as_posix(),
            "TELEGRAM_EVENT_FEED_FILE": (state() / "incoming_feed.jsonl").as_posix(),
            "TELEGRAM_TRANSCRIBE": "on-demand",
            "TELEGRAM_TRANSCRIBE_ENGINE": "telegram",
        }
    )


def token() -> str:
    path = state() / "http-token.txt"
    return path.read_text(encoding="ascii").strip()


def create_token() -> None:
    path = state() / "http-token.txt"
    if not path.exists():
        atomic_write(path, secrets.token_urlsafe(48))


def port() -> int:
    p = state() / "service-settings.json"
    return int(json.loads(p.read_text(encoding="utf-8")).get("port", 8765)) if p.exists() else 8765


def options() -> dict:
    path = state() / "options.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_options(changes: dict) -> None:
    current = options()
    current.update(changes)
    atomic_write(state() / "options.json", json.dumps(current, indent=2))


def install_binary() -> None:
    prepare()
    if getattr(sys, "frozen", False):
        source = Path(sys.executable).resolve()
        if source != executable().resolve():
            temporary = executable().with_suffix(".new.exe")
            shutil.copy2(source, temporary)
            os.replace(temporary, executable())
        shortcut = (
            Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Telegram MCP.lnk"
        )
        shortcut.parent.mkdir(parents=True, exist_ok=True)

        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"

        script = (
            "$shell = New-Object -ComObject WScript.Shell; "
            f"$link = $shell.CreateShortcut({quote(shortcut)}); "
            f"$link.TargetPath = {quote(executable())}; "
            f"$link.WorkingDirectory = {quote(root())}; "
            "$link.Description = 'Telegram MCP settings'; $link.Save()"
        )
        run_hidden(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True
        )
    create_token()


def client_paths(home: Path | None = None, appdata: Path | None = None) -> dict[str, Path]:
    home = home or Path.home()
    appdata = appdata or Path(os.environ["APPDATA"])
    return {
        "codex": Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "config.toml",
        "claude-desktop": appdata / "Claude" / "claude_desktop_config.json",
        "claude-code": home / ".claude.json",
    }


def merge_codex(text: str, invocation: list[str]) -> str:
    import tomlkit

    before = tomllib.loads(text)
    document = tomlkit.parse(text)
    if "mcp_servers" not in document:
        document["mcp_servers"] = tomlkit.table()
    entry = {
        "command": invocation[0],
        "args": invocation[1:],
        "enabled": True,
        "startup_timeout_sec": 60,
        "tool_timeout_sec": 90,
    }
    old = before.get("mcp_servers", {}).get("telegram", {})
    # Preserve existing tool restrictions and approval behavior.
    for key in (
        "enabled_tools",
        "disabled_tools",
        "required",
        "default_tools_approval_mode",
        "tools",
    ):
        if key in old:
            entry[key] = old[key]
    if "default_tools_approval_mode" not in old:
        entry["default_tools_approval_mode"] = "writes"
    document["mcp_servers"]["telegram"] = entry
    after_text = tomlkit.dumps(document)
    after = tomllib.loads(after_text)
    old_other = json.loads(json.dumps(before))
    new_other = json.loads(json.dumps(after))
    old_other.get("mcp_servers", {}).pop("telegram", None)
    new_other.get("mcp_servers", {}).pop("telegram", None)
    if not old_other.get("mcp_servers"):
        old_other.pop("mcp_servers", None)
    if not new_other.get("mcp_servers"):
        new_other.pop("mcp_servers", None)
    if old_other != new_other:
        raise ValueError("Не удалось сохранить остальные настройки Codex.")
    return after_text


def merge_claude(text: str, invocation: list[str]) -> str:
    data = json.loads(text) if text.strip() else {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Поле mcpServers должно содержать объект JSON.")
    old = servers.get("telegram", {})
    entry = {
        key: value
        for key, value in old.items()
        if key not in {"command", "args", "env", "type", "url", "headers"}
    }
    entry.update({"command": invocation[0], "args": invocation[1:]})
    servers["telegram"] = entry
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def configure_clients(selected: list[str], paths: dict | None = None) -> list[str]:
    paths = paths or client_paths()
    invocation = command("--stdio")
    planned = []
    for name in selected:
        path = paths[name]
        original = path.read_bytes() if path.exists() else None
        text = original.decode("utf-8-sig") if original is not None else ""
        updated = (
            merge_codex(text, invocation) if name == "codex" else merge_claude(text, invocation)
        )
        planned.append((path, original, updated))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    written = []
    try:
        for path, original, updated in planned:
            now = path.read_bytes() if path.exists() else None
            if now != original:
                raise RuntimeError("Настройки изменились во время установки. Повторите установку.")
            if original is not None:
                backup = path.with_name(path.name + f".before-telegram-{stamp}.bak")
                backup.write_bytes(original)
            atomic_write(path, updated)
            written.append((path, original, updated.encode("utf-8")))
        save_options({"clients": selected})
    except Exception:
        for path, original, our_version in reversed(written):
            if path.read_bytes() == our_version:
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)
        raise
    return [str(item[0]) for item in planned]


def register_task(autostart: bool = True) -> None:
    from xml.sax.saxutils import escape

    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    sid = user_sid()
    trigger = (
        f"<LogonTrigger><Enabled>true</Enabled><UserId>{sid}</UserId></LogonTrigger>"
        if autostart
        else ""
    )
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Shared local Telegram MCP server</Description></RegistrationInfo>
<Triggers>{trigger}</Triggers>
<Principals><Principal id="Author"><UserId>{sid}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Enabled>true</Enabled><AllowStartOnDemand>true</AllowStartOnDemand></Settings>
<Actions Context="Author"><Exec><Command>{escape(str(executable()))}</Command><Arguments>--serve</Arguments><WorkingDirectory>{escape(str(root()))}</WorkingDirectory></Exec></Actions>
</Task>"""
    path = state() / "server-task.xml"
    path.write_text(xml, encoding="utf-16")
    result = run_hidden(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
    if result.returncode:
        raise RuntimeError("Windows не разрешила зарегистрировать задачу запуска сервера.")
    atomic_write(state() / "task-registered", "yes\n")
    save_options({"autostart": autostart})
