"""Regression checks for the Windows 1.0.0 review. No real Telegram activity."""

import asyncio
from contextlib import nullcontext
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from windows_app import auth, security, service, settings


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEX_TELEGRAM_MCP_HOME", str(tmp_path / "app"))
    settings.state().mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        r"\\example.invalid\share\x",
        "//example.invalid/x",
        r"\\?\C:\x",
        r"\\.\PIPE\x",
        r"/??/UNC/example.invalid/x",
    ],
)
@pytest.mark.asyncio
async def test_network_paths_are_rejected_without_resolution(private, monkeypatch, path):
    from telegram_mcp import runtime

    root = private.resolve()
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [root])

    def never_resolve(*args, **kwargs):
        raise AssertionError("Untrusted path reached filesystem resolution")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", never_resolve)
        read, error = await runtime._resolve_readable_file_path(
            raw_path=path, ctx=None, tool_name="send_file"
        )
        assert read is None and error
        write, error = await runtime._resolve_writable_file_path(
            raw_path=path, default_filename="x", ctx=None, tool_name="download_media"
        )
        assert write is None and error
        with pytest.raises(ValueError):
            runtime._coerce_root_uri_to_path("file://example.invalid/share/x")


def test_msix_and_classic_settings_are_updated_without_losing_preferences(private, monkeypatch):
    appdata = private / "Roaming"
    local = private / "Local"
    package = local / "Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"
    package.mkdir(parents=True)
    config = package / "claude_desktop_config.json"
    config.write_text(
        json.dumps({"preferences": {"keep": True}, "mcpServers": {"other": {"command": "keep"}}})
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(settings, "client_paths", lambda: {"claude-desktop": config})
    settings.configure_clients(["claude-desktop"])
    assert json.loads(config.read_text())["preferences"] == {"keep": True}
    assert "telegram" in json.loads(config.read_text())["mcpServers"]
    classic = appdata / "Claude/claude_desktop_config.json"
    assert "telegram" in json.loads(classic.read_text())["mcpServers"]
    backups = list(package.glob("*.bak"))
    settings.configure_clients(["claude-desktop"])
    assert list(package.glob("*.bak")) == backups
    settings.configure_clients([])
    parsed = json.loads(config.read_text())
    assert parsed["mcpServers"] == {"other": {"command": "keep"}}


def test_environment_is_bound_to_private_file_and_root(private, monkeypatch):
    monkeypatch.chdir(private)
    (private / ".env").write_text(
        "TELEGRAM_SESSION_STRING=wrong-account\nTELEGRAM_ALLOWED_ROOTS=C:/\n"
    )
    settings.save(
        {
            "TELEGRAM_API_ID": "123",
            "TELEGRAM_API_HASH": "local",
            "TELEGRAM_EXPECTED_USER_ID": "456",
        }
    )
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "inherited-account")
    monkeypatch.setenv("TELEGRAM_API_ID_OTHER", "789")
    monkeypatch.setenv("TELEGRAM_ALLOWED_ROOTS", "C:/")
    with monkeypatch.context() as patch:
        # Restore every environment variable changed by the production loader.
        patch.setattr(os, "environ", dict(os.environ))
        service.configure_environment()
        assert "TELEGRAM_SESSION_STRING" not in os.environ
        assert "TELEGRAM_API_ID_OTHER" not in os.environ
        assert os.environ["TELEGRAM_MCP_ENV_FILE"] == str(settings.env_path())
        assert os.environ["TELEGRAM_ALLOWED_ROOTS"] == ""
        assert os.environ["TELEGRAM_EXPECTED_USER_ID"] == "456"
        assert Path.cwd() == settings.state()


def test_failed_binary_replacement_restores_previous_version(private, monkeypatch):
    target = private / "app.exe"
    temporary = private / "new.exe"
    target.write_bytes(b"previous")
    temporary.write_bytes(b"updated")
    original = Path.rename

    def fail_new(self, destination):
        if self == temporary:
            raise PermissionError("simulated replacement failure")
        return original(self, destination)

    monkeypatch.setattr(Path, "rename", fail_new)
    with pytest.raises(PermissionError):
        settings.replace_binary(temporary, target)
    assert target.read_bytes() == b"previous"


@pytest.mark.asyncio
async def test_invalid_code_and_password_retry_without_resending_code(private, monkeypatch):
    from telethon import errors

    connection = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(side_effect=[False, True]),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="same-hash")),
        sign_in=AsyncMock(
            side_effect=[
                errors.PhoneCodeInvalidError(None),
                errors.SessionPasswordNeededError(None),
                errors.PasswordHashInvalidError(None),
                None,
            ]
        ),
        get_me=AsyncMock(return_value=SimpleNamespace(id=42, username=None, bot=False)),
    )
    monkeypatch.setattr(auth, "session_lock", nullcontext)
    monkeypatch.setattr(auth, "client", lambda: connection)
    answers = iter(["wrong-code", "correct-code", "wrong-password", "correct-password"])
    await auth.authorize("phone", "+10000000000", lambda *args: next(answers), lambda png: None)
    assert connection.send_code_request.await_count == 1
    assert connection.sign_in.await_args_list[0].kwargs["phone_code_hash"] == "same-hash"
    assert connection.sign_in.await_args_list[1].kwargs["phone_code_hash"] == "same-hash"
    assert settings.values()["TELEGRAM_EXPECTED_USER_ID"] == "42"


def test_recycled_pid_is_not_an_owned_backend(private):
    record = security.identity()
    record["created"] -= 1
    assert security.matching_process(record) is None


@pytest.mark.asyncio
async def test_websocket_is_denied_before_upstream():
    upstream = AsyncMock()
    send = AsyncMock()
    control = service.LocalControl(upstream, "secret", lambda: {}, lambda: None)
    await control({"type": "websocket"}, AsyncMock(), send)
    upstream.assert_not_awaited()
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})


@pytest.mark.asyncio
async def test_account_without_username_is_checked_by_id(monkeypatch):
    from telegram_mcp import runner

    monkeypatch.setenv("TELEGRAM_EXPECTED_USER_ID", "42")
    connection = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=99, username=None)),
        disconnect=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="account ID"):
        await runner._verify_expected_username("default", connection)
    connection.disconnect.assert_awaited_once()


def test_wrong_local_server_never_receives_bearer_token(private, monkeypatch):
    security.prepare_endpoint()
    settings.create_token()
    attacker = private / "untrusted"
    (attacker / "state").mkdir(parents=True)
    with monkeypatch.context() as patch:
        patch.setenv("ALEX_TELEGRAM_MCP_HOME", str(attacker))
        security.prepare_endpoint()
        wrong_cert, wrong_key = security.certificate(), security.private_key()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(wrong_cert, wrong_key)
    received = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)
        settings.atomic_write(
            settings.state() / "service-settings.json",
            json.dumps({"port": listener.getsockname()[1]}),
        )

        def peer():
            connection, _ = listener.accept()
            with connection:
                try:
                    with tls.wrap_socket(connection, server_side=True) as stream:
                        received.append(stream.recv(8192))
                except OSError:
                    pass

        thread = threading.Thread(target=peer)
        thread.start()
        assert service.status() is None
        thread.join(timeout=6)
        assert not thread.is_alive()
    assert not received


def test_login_session_lock_blocks_a_second_owner_before_sqlite(private, monkeypatch):
    from telegram_mcp import singleton

    original = singleton.SessionLock.__init__

    def isolated(self, label, session_identity):
        original(self, label, session_identity, lock_dir=private / "locks")

    monkeypatch.setattr(singleton.SessionLock, "__init__", isolated)
    with auth.session_lock():
        with pytest.raises(singleton.SessionLockError):
            with auth.session_lock():
                pytest.fail("Second owner entered a locked Telegram session")


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity regression")
def test_unresponsive_startup_is_stopped_by_verified_process_record(private, monkeypatch):
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=str(repo))
    code = (
        "from windows_app import security; import time; security.record_process(); time.sleep(120)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code], env=env, creationflags=settings.CREATE_HIDDEN
    )
    try:
        deadline = time.monotonic() + 8
        while not security.recorded_processes():
            assert time.monotonic() < deadline
            time.sleep(0.1)
        monkeypatch.setattr(service, "status", lambda: None)
        service.stop_service()
        child.wait(timeout=8)
        assert not security.recorded_processes()
    finally:
        if child.poll() is None:
            settings.run_hidden(["taskkill", "/PID", str(child.pid), "/T", "/F"])
            child.wait(timeout=8)


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS reparse-point regression")
@pytest.mark.asyncio
async def test_local_junction_is_rejected_before_resolve(private, monkeypatch):
    from telegram_mcp import runtime

    root = private.resolve()
    target = root / "destination"
    target.mkdir()
    link = root / "junction"
    result = settings.run_hidden(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)])
    assert result.returncode == 0
    monkeypatch.setattr(runtime, "SERVER_ALLOWED_ROOTS", [root])
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", lambda *a, **kw: pytest.fail("Reparse point was resolved"))
        result, error = await runtime._resolve_writable_file_path(
            raw_path=str(link / "x"), default_filename="x", ctx=None, tool_name="download_media"
        )
        assert result is None and "reparse" in error


@pytest.mark.asyncio
async def test_failed_logout_retains_credentials(private, monkeypatch):
    settings.save({"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "test-only"})
    connection = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        log_out=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(auth, "session_lock", nullcontext)
    monkeypatch.setattr(auth, "client", lambda: connection)
    with pytest.raises(RuntimeError, match="Данные сохранены"):
        await auth.logout()
    assert settings.values()["TELEGRAM_API_HASH"] == "test-only"


def test_uninstall_stops_only_owned_stdio_bridges(private, monkeypatch):
    from unittest.mock import Mock

    own_bridge = Mock()
    own_bridge.exe.return_value = str(settings.executable())
    own_bridge.username.return_value = "owner"
    own_bridge.cmdline.return_value = [str(settings.executable()), "--stdio"]
    own_bridge.pid = 42
    other = Mock()
    other.exe.return_value = str(private / "OtherApp.exe")
    owner = Mock()
    owner.username.return_value = "owner"
    monkeypatch.setattr(security.psutil, "Process", lambda: owner)
    monkeypatch.setattr(security.psutil, "process_iter", lambda: [other, own_bridge])
    monkeypatch.setattr(security, "identity", lambda pid: {"pid": pid})
    monkeypatch.setattr(security, "matching_process", lambda record: own_bridge)
    security.stop_bridges()
    own_bridge.kill.assert_called_once()
    other.kill.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows access control")
def test_private_windows_directory_acl_contains_only_user_and_system(private):
    from windows_acl import read_acl

    settings.prepare()
    acl = read_acl(settings.root())
    assert acl["protected"] is True
    assert set(acl["ids"]) == {settings.user_sid(), "S-1-5-18"}
