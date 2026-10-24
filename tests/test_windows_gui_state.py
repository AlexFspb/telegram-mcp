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
    app.start_button, app.stop_button, app.other_button = Button(), Button(), Button()
    app.buttons = [app.start_button, app.stop_button, app.other_button]
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
