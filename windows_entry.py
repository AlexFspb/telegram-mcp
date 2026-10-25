"""Single EXE entry: setup GUI, shared daemon, or MCP transport bridge."""

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import shutil
import sys

from windows_app import settings, service, windows_stdio


def upgrade_current():
    with service.local_lock("bootstrap.lock", timeout=180):
        if not settings.env_path().exists():
            raise RuntimeError("Установка не найдена. Откройте установщик для входа.")
        service.stop_service()
        settings.install_binary()
        settings.register_task(settings.options().get("autostart", True))
        settings.configure_clients(
            settings.options().get("clients", ["codex", "claude-desktop", "claude-code"])
        )
    print(json.dumps(service.ensure_service(explicit=True)))


def migrate_legacy(folder: Path):
    from dotenv import dotenv_values

    old = dotenv_values(folder / "state" / ".env")
    source = Path(old["TELEGRAM_SESSION_NAME"] + ".session")
    if not source.is_absolute():
        source = (folder / source).resolve()
    service.stop_service()
    settings.install_binary()
    destination = settings.state() / "telegram.session"
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        if destination.exists():
            with sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True) as existing:
                identity = original.execute("SELECT dc_id, auth_key FROM sessions").fetchone()
                saved_identity = existing.execute(
                    "SELECT dc_id, auth_key FROM sessions"
                ).fetchone()
            if not identity or not identity[1] or identity != saved_identity:
                raise RuntimeError("Новая установка содержит другой сеанс. Перенос не выполнен.")
        else:
            with sqlite3.connect(destination) as target:
                original.backup(target)
    settings.save_credentials(old["TELEGRAM_API_ID"], old["TELEGRAM_API_HASH"])
    aliases = folder / "state" / "aliases.json"
    if aliases.exists() and not (settings.state() / "aliases.json").exists():
        shutil.copy2(aliases, settings.state() / "aliases.json")
    for source_folder, target_folder in (
        (folder / "state" / "transcripts", settings.state() / "transcripts"),
        (folder / "files", settings.root() / "files"),
    ):
        if source_folder.exists():
            for item in source_folder.rglob("*"):
                target = target_folder / item.relative_to(source_folder)
                if item.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
    settings.save({"TELEGRAM_EXPECTED_USERNAME": old.get("TELEGRAM_EXPECTED_USERNAME", "")})
    settings.register_task(True)
    settings.configure_clients(["codex", "claude-desktop", "claude-code"])
    print(json.dumps(service.ensure_service(explicit=True)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--migrate-legacy", type=Path)
    args = parser.parse_args()
    if args.self_check:
        import tkinter
        from importlib import metadata
        from telegram_mcp.install_guard import assert_safe_distribution

        assert_safe_distribution()
        interpreter = tkinter.Tcl()  # Tcl only: no window or screen access.
        library = Path(interpreter.eval("info library"))
        bundled = not getattr(sys, "frozen", False) or library.is_relative_to(Path(sys._MEIPASS))
        assert bundled, "Tcl data was not bundled"
        provenance = json.loads(metadata.distribution("telegram-mcp").read_text("direct_url.json"))
        print(
            json.dumps(
                {
                    "tcl": interpreter.eval("info patchlevel"),
                    "tcl_bundled": bundled,
                    "source": provenance["url"],
                    "console_attached": windows_stdio.console_attached(),
                    "pe_subsystem": (
                        windows_stdio.pe_subsystem(Path(sys.executable))
                        if getattr(sys, "frozen", False)
                        else None
                    ),
                }
            )
        )
    elif args.upgrade:
        upgrade_current()
    elif args.serve:
        service.serve()
    elif args.stdio:
        asyncio.run(service.bridge())
    elif args.status:
        print(json.dumps(service.status() or {"running": False}))
    elif args.start:
        print(json.dumps(service.ensure_service(explicit=True)))
    elif args.stop:
        service.stop_service()
        print(json.dumps({"stopped": True}))
    elif args.migrate_legacy:
        migrate_legacy(args.migrate_legacy)
    else:
        from windows_app.gui import main as gui_main

        gui_main()


if __name__ == "__main__":
    try:
        windows_stdio.configure_standard_streams(require_protocol="--stdio" in sys.argv)
        main()
    except Exception as error:
        print(f"Telegram MCP: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
