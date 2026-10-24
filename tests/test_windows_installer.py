"""Offline Windows installer checks; no UI or real Telegram account is used."""

import json
from pathlib import Path

import httpx
import pytest

from windows_app import service, settings

tomllib = settings.tomllib


@pytest.fixture
def local_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEX_TELEGRAM_MCP_HOME", str(tmp_path / "private"))
    return tmp_path


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quoted", [False, True])
def test_codex_merge_preserves_other_servers_and_security_policies(newline, quoted):
    source = """# keep this comment
model = "existing"
[mcp_servers.other]
command = "old"
[mcp_servers.telegram]
command = "legacy"
args = ["old.py"]
default_tools_approval_mode = "prompt"
disabled_tools = ["delete_message"]
[mcp_servers.telegram.env]
OLD_SECRET = "must-not-be-kept"
[mcp_servers.telegram.tools.send_message]
approval_mode = "prompt"
[unrelated]
keep = true
""".replace("\n", newline)
    if quoted:
        source = source.replace("[mcp_servers.", '["mcp_servers".')
    invocation = [r"C:\User Name\TelegramMCP.exe", "--stdio"]
    updated = settings.merge_codex(source, invocation)
    parsed = tomllib.loads(updated)
    assert parsed["model"] == "existing"
    assert parsed["mcp_servers"]["other"] == {"command": "old"}
    entry = parsed["mcp_servers"]["telegram"]
    assert entry["command"] == invocation[0]
    assert entry["args"] == ["--stdio"]
    assert entry["default_tools_approval_mode"] == "prompt"
    assert entry["disabled_tools"] == ["delete_message"]
    assert entry["tools"]["send_message"]["approval_mode"] == "prompt"
    assert "env" not in entry
    assert "# keep this comment" in updated
    assert settings.merge_codex(updated, invocation) == updated


def test_claude_merge_keeps_unrelated_configuration():
    source = {"otherSetting": {"nested": 2}, "mcpServers": {"existing": {"command": "keep"}}}
    merged = json.loads(
        settings.merge_claude(json.dumps(source), [r"C:\App Dir\a.exe", "--stdio"])
    )
    assert merged["otherSetting"] == source["otherSetting"]
    assert merged["mcpServers"]["existing"] == source["mcpServers"]["existing"]
    assert "env" not in merged["mcpServers"]["telegram"]


def test_invalid_client_file_prevents_all_writes(local_home):
    paths = {
        name: local_home / (name + ".config")
        for name in ("codex", "claude-desktop", "claude-code")
    }
    paths["codex"].write_text('model = "unchanged"\n', encoding="utf-8")
    paths["claude-code"].write_text("invalid JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        settings.configure_clients(list(paths), paths)
    assert paths["codex"].read_text() == 'model = "unchanged"\n'
    assert not paths["claude-desktop"].exists()


def test_config_write_failure_rolls_back_previous_writes(local_home, monkeypatch):
    paths = {
        name: local_home / (name + ".config")
        for name in ("codex", "claude-desktop", "claude-code")
    }
    paths["codex"].write_text('model = "unchanged"\n', encoding="utf-8")
    original_write = settings.atomic_write

    def fail_last(path, text):
        if path == paths["claude-code"]:
            raise PermissionError("test")
        original_write(path, text)

    monkeypatch.setattr(settings, "atomic_write", fail_last)
    with pytest.raises(PermissionError):
        settings.configure_clients(list(paths), paths)
    assert paths["codex"].read_text() == 'model = "unchanged"\n'
    assert not paths["claude-desktop"].exists()


def test_settings_roundtrip_and_no_secrets_in_client_configs(local_home):
    settings.state().mkdir(parents=True)
    settings.save({"TEST_SECRET": "a 'quote' and a \\ slash"})
    assert settings.values()["TEST_SECRET"] == "a 'quote' and a \\ slash"
    settings.save({"TEST_SECRET": "changed"})
    assert settings.values()["TEST_SECRET"] == "changed"
    assert settings.env_path().read_text().count("TEST_SECRET=") == 1
    assert "TEST_SECRET" not in settings.merge_codex("", ["app.exe", "--stdio"])
    assert "TEST_SECRET" not in settings.merge_claude("{}", ["app.exe", "--stdio"])


@pytest.mark.asyncio
async def test_http_control_requires_token_and_preserves_underlying_protocol():
    from starlette.responses import JSONResponse

    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        await JSONResponse({"protocol": "forwarded"})(scope, receive, send)

    stopped = []
    control = service.LocalControl(
        app, "private-test-token", lambda: {"service": "test"}, lambda: stopped.append(True)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.post("/control/shutdown")).status_code == 401
        assert not stopped
        headers = {"Authorization": "Bearer private-test-token"}
        assert (await client.get("/health", headers=headers)).json() == {"service": "test"}
        assert (await client.post("/mcp", headers=headers)).json() == {"protocol": "forwarded"}
        assert calls == ["/mcp"]
        assert (await client.post("/control/shutdown", headers=headers)).status_code == 200
        assert stopped == [True]


def test_setup_complete_requires_saved_authorization_and_client_registration(local_home):
    settings.state().mkdir(parents=True)
    settings.executable().write_bytes(b"test-only-executable")
    settings.save({"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "test-only"})
    assert not settings.setup_complete()
    (settings.state() / "telegram.session").write_bytes(b"test-only-session")
    (settings.state() / "task-registered").write_text("yes")
    settings.save_options({"clients": []})
    assert not settings.setup_complete()
    settings.save({"TELEGRAM_EXPECTED_USER_ID": "42"})
    assert settings.setup_complete()
    # Stopping a backend leaves the installation and authorization intact.
    (settings.state() / "stopped").write_text("yes")
    assert settings.setup_complete()
    settings.env_path().unlink()
    assert not settings.setup_complete()
