"""Two separate MCP clients must share a backend and preserve image content."""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from windows_app import service, settings


@pytest.mark.asyncio
async def test_two_stdio_processes_share_backend_and_survive_peer_exit(tmp_path, monkeypatch):
    root = tmp_path / "local"
    (root / "state").mkdir(parents=True)
    (root / "state" / "http-token.txt").write_text("acceptance-test-token", encoding="ascii")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (root / "state" / "service-settings.json").write_text(json.dumps({"port": port}))
    monkeypatch.setenv("ALEX_TELEGRAM_MCP_HOME", str(root))
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=str(repo))
    with open(tmp_path / "fixture.log", "w") as log:
        server = subprocess.Popen(
            [sys.executable, str(repo / "tests" / "windows_fake_backend.py")],
            env=env,
            stdout=log,
            stderr=log,
            creationflags=settings.CREATE_HIDDEN,
        )
        try:
            deadline = time.monotonic() + 12
            while not service.status():
                if time.monotonic() > deadline:
                    pytest.fail("Acceptance fixture did not start")
                await asyncio.sleep(0.1)
            backend_pid = service.status()["pid"]

            exe = os.environ.get("TELEGRAM_TEST_EXE")
            command = exe or sys.executable
            args = ["--stdio"] if exe else [str(repo / "windows_entry.py"), "--stdio"]
            params = StdioServerParameters(command=command, args=args, env=env)
            both_ready = asyncio.Event()
            first_closed = asyncio.Event()
            first_ready = asyncio.Event()

            async def first():
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize()
                        result = await client.call_tool("ping", {"label": "first"})
                        first_ready.set()
                        await both_ready.wait()
                        parsed = result.structuredContent or json.loads(result.content[0].text)
                        assert parsed["pid"] == backend_pid
                first_closed.set()

            async def second():
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as client:
                        await client.initialize()
                        await first_ready.wait()
                        both_ready.set()
                        await first_closed.wait()
                        result = await client.call_tool("ping", {"label": "second"})
                        parsed = result.structuredContent or json.loads(result.content[0].text)
                        assert parsed == {"label": "second", "pid": backend_pid}
                        picture = await client.call_tool("image", {})
                        assert any(block.type == "image" for block in picture.content)

            await asyncio.wait_for(asyncio.gather(first(), second()), timeout=50)
            assert server.poll() is None
        finally:
            if os.name == "nt":
                settings.run_hidden(["taskkill", "/PID", str(server.pid), "/T", "/F"])
            else:
                server.terminate()
            server.wait(timeout=10)
