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

from windows_app import VERSION, security, settings

SERVICE_NAME = "alex-telegram-mcp"


class BackendLogStream:
    """Text stream used by logging and uvicorn's terminal-color detection."""

    encoding = "utf-8"

    def __init__(self, handler):
        self.handler = handler

    def write(self, text):
        import logging

        if text.strip():
            logging.getLogger("backend").warning(text.rstrip())
        return len(text)

    def flush(self):
        self.handler.flush()

    def isatty(self):
        return False


def base_url() -> str:
    return f"https://127.0.0.1:{settings.port()}"


def request(path: str, method: str = "GET", timeout: float = 2.0):
    auth = settings.token()
    req = urllib.request.Request(
        base_url() + path, method=method, headers={"Authorization": "Bearer " + auth}
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=security.tls_context()),
    )
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


def ensure_service(explicit=False):  # noqa: C901
    if explicit:
        (settings.state() / "stopped").unlink(missing_ok=True)
    elif (settings.state() / "stopped").exists():
        raise RuntimeError(
            "Сервер остановлен пользователем. Запустите его через значок или настройки."
        )
    current = status()
    if current:
        return current
    with local_lock("bootstrap.lock"):
        if (settings.state() / "stopped").exists():
            raise RuntimeError("Сервер остановлен пользователем.")
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


def stop_service():  # noqa: C901 - graceful shutdown and verified-process fallback
    settings.atomic_write(settings.state() / "stopped", "yes\n")
    current = status()
    if not current and not security.recorded_processes():
        _stop_legacy_server()
    if current:
        try:
            request("/control/shutdown", method="POST")
        except OSError:
            pass
    processes = security.recorded_processes()
    deadline = time.monotonic() + (25 if current else 2)
    while time.monotonic() < deadline:
        if not any(process.is_running() for process in processes):
            return
        time.sleep(0.2)
    # A PID alone is insufficient: recorded creation time and image must match.
    for process in security.recorded_processes():
        try:
            process.kill()
            process.wait(timeout=8)
        except security.psutil.NoSuchProcess:
            pass
    if security.recorded_processes():
        raise RuntimeError("Не удалось остановить сервер. Вход в сеанс не начат.")


def _stop_legacy_server():
    """Upgrade 1.0.0 only after verifying the listener's executable and user."""
    import psutil

    expected = os.path.normcase(str(settings.executable().resolve()))
    for endpoint in psutil.net_connections(kind="tcp4"):
        if endpoint.status != psutil.CONN_LISTEN or not endpoint.laddr:
            continue
        if endpoint.laddr.ip != "127.0.0.1" or endpoint.laddr.port != settings.port():
            continue
        process = psutil.Process(endpoint.pid)
        if (
            os.path.normcase(process.exe()) != expected
            or process.username() != psutil.Process().username()
        ):
            raise RuntimeError("Порт занят другим процессом. Обновление остановлено.")
        record = security.identity(process.pid)
        parent = process.parent()
        records = [record]
        if parent and os.path.normcase(parent.exe()) == expected:
            records.append(security.identity(parent.pid))
        settings.atomic_write(settings.state() / "backend-process.json", json.dumps(records))
        # The exact owned 1.0.0 image is verified; no arbitrary HTTP endpoint gets a token.
        req = urllib.request.Request(
            f"http://127.0.0.1:{settings.port()}/control/shutdown",
            method="POST",
            headers={"Authorization": "Bearer " + settings.token()},
        )
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                req, timeout=2
            ).close()
        except OSError:
            pass
        return


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


async def bridge():  # noqa: C901 - JSON/SSE forwarding with per-request failure isolation
    """Independent stateless requests survive restarts; never replay a sent call."""
    import anyio
    import httpx
    from httpx_sse import EventSource
    from mcp import types
    from mcp.server.stdio import stdio_server
    from mcp.shared.message import SessionMessage

    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + settings.token()},
        timeout=httpx.Timeout(90, read=120),
        trust_env=False,
        verify=security.tls_context(),
    ) as http:
        async with stdio_server() as (local_in, local_out):
            protocol = None

            async def deliver(message, initializing):
                nonlocal protocol
                if initializing and isinstance(message.root, types.JSONRPCResponse):
                    protocol = message.root.result.get("protocolVersion")
                await local_out.send(SessionMessage(message))

            async def forward(frame):
                message = frame.message.root
                initializing = (
                    isinstance(message, types.JSONRPCRequest) and message.method == "initialize"
                )
                try:
                    await asyncio.to_thread(ensure_service)
                    async with http.stream(
                        "POST",
                        base_url() + "/mcp",
                        headers={
                            "Accept": "application/json, text/event-stream",
                            **(
                                {"MCP-Protocol-Version": protocol}
                                if protocol and not initializing
                                else {}
                            ),
                        },
                        json=frame.message.model_dump(by_alias=True, exclude_none=True),
                    ) as response:
                        response.raise_for_status()
                        if response.status_code == 202:
                            return
                        if response.headers.get("content-type", "").startswith("application/json"):
                            await response.aread()
                            await deliver(
                                types.JSONRPCMessage.model_validate(response.json()), initializing
                            )
                        else:
                            async for event in EventSource(response).aiter_sse():
                                if event.event == "message" and event.data:
                                    await deliver(
                                        types.JSONRPCMessage.model_validate_json(event.data),
                                        initializing,
                                    )
                except (httpx.HTTPError, OSError, RuntimeError, ValueError):
                    if isinstance(message, types.JSONRPCRequest):
                        await local_out.send(
                            SessionMessage(
                                types.JSONRPCMessage(
                                    types.JSONRPCError(
                                        jsonrpc="2.0",
                                        id=message.id,
                                        error=types.ErrorData(
                                            code=-32000,
                                            message=(
                                                "Связь с сервером прервана. Запрос не повторён. "
                                                "Результат операции может быть неизвестен. Проверьте его перед повтором."
                                            ),
                                        ),
                                    )
                                )
                            )
                        )

            async with anyio.create_task_group() as tasks:
                async for frame in local_in:
                    if not isinstance(frame, Exception):
                        tasks.start_soon(forward, frame)
                tasks.cancel_scope.cancel()


class LocalControl:
    """Keep the loopback endpoint private and expose a content-free health check."""

    def __init__(self, app, token: str, health, shutdown):
        self.app, self.token, self.health, self.shutdown = app, token, health, shutdown

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
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


def _tray(shutdown, ready: threading.Event, stopped: threading.Event):
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
        settings.atomic_write(settings.state() / "stopped", "yes\n")
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

        def monitor():
            stopped.wait()
            icon.stop()

        threading.Thread(target=monitor, daemon=True).start()

    icon.run(setup=setup)


def configure_environment():
    # Credentials belong to this installation, never to an inherited client environment.
    for name in list(os.environ):
        if name.startswith(("TELEGRAM_", "MCP_")):
            os.environ.pop(name, None)
    os.environ.update(
        {key: value for key, value in settings.values().items() if value is not None}
    )
    os.environ["TELEGRAM_MCP_ENV_FILE"] = str(settings.env_path())
    os.environ["TELEGRAM_SESSION_NAME"] = str(settings.state() / "telegram")
    os.environ["TELEGRAM_ALLOWED_ROOTS"] = ""
    os.environ["TELEGRAM_SERVER_ROOTS_ONLY"] = "1"
    os.environ["MCP_TRANSPORT"] = "http"
    os.environ["MCP_HOST"] = "127.0.0.1"
    os.environ["MCP_PORT"] = str(settings.port())
    os.environ["MCP_ALLOWED_HOSTS"] = f"127.0.0.1:{settings.port()}"
    os.environ["MCP_ALLOWED_ORIGINS"] = ""
    os.environ["TELEGRAM_SESSION_LOCK"] = "exclusive"
    os.chdir(settings.state())


def serve():
    import logging
    from logging.handlers import RotatingFileHandler

    log_path = settings.root() / "logs" / "backend.log"
    log_path.parent.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)

    sys.stdout = sys.stderr = BackendLogStream(handler)
    (settings.state() / "stopped").unlink(missing_ok=True)
    configure_environment()
    with local_lock("backend.lock", timeout=0.5):
        security.record_process()
        security.prepare_endpoint()
        from telegram_mcp import runner
        from telegram_mcp import runtime

        async def serve_authenticated(_transport):
            import uvicorn

            if not settings.values().get("TELEGRAM_EXPECTED_USER_ID"):
                me = await runtime.clients["default"].get_me()
                settings.save({"TELEGRAM_EXPECTED_USER_ID": str(me.id)})
            runner._configure_transport_security()
            started = datetime.now(timezone.utc).isoformat()
            tray_ready = threading.Event()
            tray_stopped = threading.Event()
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
                    ssl_certfile=str(security.certificate()),
                    ssl_keyfile=str(security.private_key()),
                    access_log=False,
                    log_level="warning",
                    timeout_graceful_shutdown=5,
                )
            )
            threading.Thread(
                target=_tray, args=(shutdown, tray_ready, tray_stopped), daemon=True
            ).start()
            try:
                await server.serve()
            finally:
                tray_stopped.set()

        runner._serve = serve_authenticated
        sys.argv = ["TelegramMCP", "--transport", "http", str(settings.root() / "files")]
        runner.main()
