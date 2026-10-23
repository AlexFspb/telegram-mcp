"""One authenticated loopback MCP backend, with lightweight client bridges."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from windows_app import VERSION, settings

SERVICE_NAME = "alex-telegram-mcp"


def base_url() -> str:
    return f"http://127.0.0.1:{settings.port()}"


def request(path: str, method: str = "GET", timeout: float = 2.0):
    auth = settings.token()
    req = urllib.request.Request(
        base_url() + path, method=method, headers={"Authorization": "Bearer " + auth}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read())


def status():
    try:
        result = request("/health")
        if result.get("service") == SERVICE_NAME:
            return result
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return None


@contextmanager
def local_lock(name: str, timeout: float = 45):
    import msvcrt

    path = settings.state() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Другой процесс выполняет запуск сервера.") from None
                time.sleep(0.15)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_service():
    current = status()
    if current:
        return current
    with local_lock("bootstrap.lock"):
        current = status()
        if current:
            return current
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", settings.port())) == 0:
                raise RuntimeError("Локальный порт сервера занят другим приложением.")
        if (settings.state() / "task-registered").exists():
            result = settings.run_hidden(["schtasks", "/Run", "/TN", settings.TASK_NAME])
            if result.returncode:
                raise RuntimeError("Windows не смогла запустить задачу Telegram MCP.")
        else:
            # Source/development use. Installed copies use Task Scheduler so a
            # client's process-tree cleanup cannot kill the shared backend.
            with open(settings.root() / "logs" / "backend.log", "ab", buffering=0) as log:
                subprocess.Popen(
                    settings.command("--serve"),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=settings.root(),
                    creationflags=settings.CREATE_HIDDEN
                    | getattr(subprocess, "DETACHED_PROCESS", 0),
                    close_fds=True,
                )
        deadline = time.monotonic() + 42
        while time.monotonic() < deadline:
            current = status()
            if current:
                return current
            time.sleep(0.3)
        raise RuntimeError("Сервер не ответил. Проверьте журнал backend.log в папке приложения.")


def stop_service():
    current = status()
    if current:
        request("/control/shutdown", method="POST")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not status() and not any(
                process_alive(pid)
                for pid in {current["pid"], current.get("launcher_pid", current["pid"])}
            ):
                return
            time.sleep(0.2)
        raise RuntimeError("Сервер ещё завершает работу.")


def process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel.CloseHandle(handle)


async def bridge():
    """Forward protocol frames, including images/errors, without duplicating tools."""
    import anyio
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server

    await asyncio.to_thread(ensure_service)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + settings.token()},
        timeout=httpx.Timeout(90, read=120),
        trust_env=False,
    ) as http:
        async with streamable_http_client(base_url() + "/mcp", http_client=http) as (
            remote_in,
            remote_out,
            _,
        ):
            async with stdio_server() as (local_in, local_out):
                async with anyio.create_task_group() as tasks:

                    async def pump(source, destination):
                        try:
                            async for message in source:
                                if isinstance(message, Exception):
                                    raise message
                                await destination.send(message)
                        finally:
                            tasks.cancel_scope.cancel()

                    tasks.start_soon(pump, local_in, remote_out)
                    tasks.start_soon(pump, remote_in, local_out)


class LocalControl:
    """Keep the loopback endpoint private and expose a content-free health check."""

    def __init__(self, app, token: str, health, shutdown):
        self.app, self.token, self.health, self.shutdown = app, token, health, shutdown

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse

        headers = dict(scope.get("headers", []))
        provided = headers.get(b"authorization", b"")
        if not hmac.compare_digest(provided, ("Bearer " + self.token).encode("ascii")):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(
                scope, receive, send
            )
        if scope["path"] == "/health" and scope["method"] == "GET":
            return await JSONResponse(self.health())(scope, receive, send)
        if scope["path"] == "/control/shutdown" and scope["method"] == "POST":
            self.shutdown()
            return await JSONResponse({"stopping": True})(scope, receive, send)
        return await self.app(scope, receive, send)


def _tray(shutdown, ready: threading.Event):
    import pystray
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 61, 61), radius=14, fill="#168bca")
    draw.rounded_rectangle((14, 16, 50, 43), radius=6, fill="white")
    draw.polygon([(20, 40), (20, 51), (31, 40)], fill="white")
    draw.ellipse((41, 41, 62, 62), fill="#22b573", outline="white", width=3)

    def open_settings(icon=None, item=None):
        subprocess.Popen(settings.command(), creationflags=settings.CREATE_HIDDEN, close_fds=True)

    def stop(icon, item):
        shutdown()
        icon.stop()

    icon = pystray.Icon(
        "AlexTelegramMCP",
        image,
        "Telegram MCP — работает",
        menu=pystray.Menu(
            pystray.MenuItem("Настройки и состояние", open_settings, default=True),
            pystray.MenuItem("Остановить сервер", stop),
        ),
    )

    def setup(icon):
        icon.visible = True
        ready.set()

    icon.run(setup=setup)


def serve():
    import logging
    from dotenv import load_dotenv

    log_path = settings.root() / "logs" / "backend.log"
    log_path.parent.mkdir(exist_ok=True)
    log = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    logging.basicConfig(level=logging.WARNING, stream=log, force=True)
    # Credentials belong to this installation, never to an inherited client environment.
    for name in list(os.environ):
        if name.startswith(
            ("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME_", "TELEGRAM_EXPECTED_USERNAME")
        ):
            os.environ.pop(name, None)
    load_dotenv(settings.env_path(), override=True)
    os.environ["MCP_TRANSPORT"] = "http"
    os.environ["MCP_HOST"] = "127.0.0.1"
    os.environ["MCP_PORT"] = str(settings.port())
    os.environ["MCP_ALLOWED_HOSTS"] = f"127.0.0.1:{settings.port()}"
    os.environ["MCP_ALLOWED_ORIGINS"] = ""
    os.environ["TELEGRAM_SESSION_LOCK"] = "exclusive"
    os.chdir(settings.root())

    with local_lock("backend.lock", timeout=0.5):
        from telegram_mcp import runner
        from telegram_mcp import runtime

        async def serve_authenticated(_transport):
            import uvicorn

            runner._configure_transport_security()
            started = datetime.now(timezone.utc).isoformat()
            tray_ready = threading.Event()
            loop = asyncio.get_running_loop()
            server = None

            def shutdown():
                def mark():
                    server.should_exit = True

                loop.call_soon_threadsafe(mark)

            def health():
                return {
                    "service": SERVICE_NAME,
                    "version": VERSION,
                    "pid": os.getpid(),
                    "launcher_pid": os.getppid() if getattr(sys, "frozen", False) else os.getpid(),
                    "started_at": started,
                    "connected": all(client.is_connected() for client in runtime.clients.values()),
                    "tray_visible": tray_ready.is_set(),
                }

            app = LocalControl(
                runtime.mcp.streamable_http_app(), settings.token(), health, shutdown
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=settings.port(),
                    access_log=False,
                    log_level="warning",
                    timeout_graceful_shutdown=10,
                )
            )
            threading.Thread(target=_tray, args=(shutdown, tray_ready), daemon=True).start()
            await server.serve()

        runner._serve = serve_authenticated
        sys.argv = ["TelegramMCP", "--transport", "http", str(settings.root() / "files")]
        runner.main()
