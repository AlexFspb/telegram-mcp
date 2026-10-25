"""Server action states, checked without opening or inspecting any window."""

import queue
from unittest.mock import Mock

import pytest

pytest.importorskip("tkinter", reason="Tkinter is required by the Windows settings module")
from windows_app.gui import Manager  # noqa: E402


class Button:
    def __init__(self):
        self.disabled = False
        self.text = ""

    def state(self, values):
        self.disabled = "disabled" in values

    def configure(self, **values):
        self.text = values.get("text", self.text)


@pytest.fixture
def manager():
    app = Manager.__new__(Manager)
    app.busy = False
    app.status_known = False
    app.backend_active = False
    app.last_status = None
    app.status_generation = 0
    app.service_action = None
    app.status_pending = False
    app.installed = False
    app.start_button, app.stop_button, app.other_button = Button(), Button(), Button()
    app.buttons = [app.start_button, app.stop_button, app.other_button]
    app.install_button, app.login_button, app.configure_button = Button(), Button(), Button()
    app.buttons.extend([app.install_button, app.login_button, app.configure_button])
    app.installation_text = Mock()
    app.credential_entries = [Button(), Button(), Button()]
    app.state_text, app.detail, app.status_label, app.progress = Mock(), Mock(), Mock(), Mock()
    app.window = Mock()
    app.events = queue.Queue()
    app.qr_window = None
    return app


def test_running_server_disables_start_and_allows_stop(manager):
    manager.apply_status({"connected": True})
    assert manager.start_button.disabled
    assert manager.start_button.text == "Сервер запущен"
    assert not manager.stop_button.disabled
    manager.state_text.set.assert_called_with("● Сервер работает. Telegram подключён.")
    manager.work = Mock()
    manager.start_server()
    manager.work.assert_not_called()


def test_stopped_server_allows_start_and_disables_stop(manager):
    manager.apply_status(None)
    assert not manager.start_button.disabled
    assert manager.stop_button.disabled
    manager.work = Mock()
    manager.stop_server()
    manager.work.assert_not_called()


def test_unknown_state_disables_both_server_actions(manager):
    manager.update_buttons()
    assert manager.start_button.disabled and manager.stop_button.disabled


def test_background_process_without_health_response_cannot_be_started_twice(manager):
    manager.apply_status(None, active=True)
    assert manager.start_button.disabled
    assert not manager.stop_button.disabled


def test_stale_status_cannot_overwrite_a_start_transition(manager):
    manager.status_generation = 2
    manager.busy = True
    manager.service_action = "start"
    manager.update_buttons()
    manager.accept_status(1, None, False)
    assert manager.start_button.text == "Запуск…"
    assert manager.start_button.disabled and manager.stop_button.disabled
    manager.state_text.set.assert_not_called()


def test_status_started_before_operation_is_ignored_after_completion(manager):
    manager.status_generation = 2
    manager.apply_status({"connected": True})
    manager.accept_status(1, None, False)
    assert manager.backend_active
    assert manager.start_button.disabled


def test_completion_keeps_start_disabled_instead_of_enabling_all_buttons(manager):
    manager.busy = True
    manager.service_action = "start"
    manager.events.put(("done", "Сервер запущен.", {"connected": True}, True))
    manager.poll()
    assert not manager.busy
    assert manager.start_button.disabled
    assert not manager.stop_button.disabled
    assert not manager.other_button.disabled
    manager.progress.stop.assert_called_once()


def test_worker_disables_actions_before_starting_and_rejects_repeated_clicks(manager, monkeypatch):
    from windows_app import gui

    thread = Mock()
    monkeypatch.setattr(gui.threading, "Thread", thread)
    manager.apply_status(None)
    manager.start_server()
    assert manager.busy
    assert manager.start_button.disabled and manager.stop_button.disabled
    manager.state_text.set.assert_called_with("Сервер запускается…")
    manager.start_server()
    assert thread.call_count == 1
    manager.progress.start.assert_called_once()


def test_installed_setup_is_disabled_even_when_server_is_stopped(manager):
    manager.apply_status(None, active=False, installed=True)
    assert manager.install_button.disabled and manager.login_button.disabled
    assert manager.install_button.text == "Установлено"
    assert manager.login_button.text == "Вход выполнен"
    assert not manager.start_button.disabled
    assert not manager.configure_button.disabled
    manager.work = Mock()
    manager.install("qr")
    manager.install("phone")
    manager.work.assert_not_called()


def test_successful_setup_stays_disabled_after_operation_completion(manager):
    manager.busy = True
    manager.events.put(("done", "Установлено.", {"connected": True}, True, True))
    manager.poll()
    assert manager.installed
    assert manager.install_button.disabled and manager.login_button.disabled
    assert manager.install_button.text == "Установлено"


def test_logout_allows_new_login_without_restarting_settings_window(manager):
    manager.apply_status({"connected": True}, installed=True)
    manager.events.put(("done", "Сеанс завершён.", None, False, False))
    manager.poll()
    assert not manager.installed
    assert not manager.install_button.disabled and not manager.login_button.disabled
    assert manager.configure_button.disabled


def test_partial_setup_still_allows_installation(manager):
    manager.apply_status(None, installed=False)
    assert not manager.install_button.disabled
    assert not manager.login_button.disabled


def test_reconfigure_saves_client_selection_and_autostart(manager, monkeypatch):
    from windows_app import gui

    manager.selected_clients = lambda: ["codex"]
    manager.autostart = Mock()
    manager.autostart.get.return_value = False
    manager.work = lambda action, success: action()
    task, clients = Mock(), Mock()
    monkeypatch.setattr(gui.settings, "register_task", task)
    monkeypatch.setattr(gui.settings, "configure_clients", clients)
    manager.reconfigure()
    task.assert_called_once_with(False)
    clients.assert_called_once_with(["codex"])


def test_cross_closes_only_settings_and_preserves_shared_server(manager, monkeypatch):
    from windows_app import gui

    stop = Mock()
    monkeypatch.setattr(gui.service, "stop_service", stop)
    manager.close_to_tray()
    manager.window.destroy.assert_called_once()
    stop.assert_not_called()


def test_cross_does_not_interrupt_in_progress_login(manager):
    manager.busy = True
    manager.close_to_tray()
    manager.window.destroy.assert_not_called()
