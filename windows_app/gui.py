"""User-facing Windows setup. All interactive input stays on the local machine."""

from __future__ import annotations

import asyncio
import io
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from windows_app import VERSION, auth, security, service, settings


class Manager:
    def __init__(self):
        self.window = tk.Tk()
        self.window.title("Telegram MCP — установка и настройки")
        self.window.geometry("680x730")
        self.window.minsize(620, 650)
        self.window.protocol("WM_DELETE_WINDOW", self.close_to_tray)
        self.events = queue.Queue()
        self.busy = False
        self.photo = None
        self.qr_window = None
        self.status_pending = False
        self.status_known = False
        self.backend_active = False
        self.last_status = None
        self.status_generation = 0
        self.service_action = None
        self.installed = settings.setup_complete()
        data = settings.values()
        self.api_id = tk.StringVar(value=data.get("TELEGRAM_API_ID", ""))
        self.api_hash = tk.StringVar(value=data.get("TELEGRAM_API_HASH", ""))
        self.phone = tk.StringVar()
        self.installation_text = tk.StringVar()
        self.state_text = tk.StringVar(value="Проверяю состояние сервера…")
        self.detail = tk.StringVar(
            value=(
                "Проверяю установленный сервер…"
                if data.get("TELEGRAM_API_ID")
                else "Заполните API-данные, затем выберите способ входа."
            )
        )
        saved_options = settings.options()
        self.autostart = tk.BooleanVar(value=saved_options.get("autostart", True))
        selected = saved_options.get("clients", list(settings.client_paths()))
        self.clients = {
            name: tk.BooleanVar(value=name in selected) for name in settings.client_paths()
        }
        self.buttons = []

        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        outer = ttk.Frame(self.window, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Telegram MCP", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(outer, text=f"Установщик и общий сервер · версия {VERSION}").pack(
            anchor="w", pady=(2, 16)
        )
        self.status_label = ttk.Label(
            outer, textvariable=self.state_text, font=("Segoe UI", 11, "bold"), wraplength=620
        )
        self.status_label.pack(anchor="w")
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(8, 0))

        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True, pady=14)
        connect = ttk.Frame(tabs, padding=16)
        manage = ttk.Frame(tabs, padding=16)
        tabs.add(connect, text="Установка и вход")
        tabs.add(manage, text="Сервер и файлы")
        if self.installed:
            tabs.select(manage)

        self.credential_entries = []
        ttk.Label(connect, text="Application API ID").pack(anchor="w")
        api_id_entry = ttk.Entry(connect, textvariable=self.api_id)
        api_id_entry.pack(fill="x", pady=(4, 10))
        self.credential_entries.append(api_id_entry)
        ttk.Label(connect, text="Application API hash").pack(anchor="w")
        api_hash_entry = ttk.Entry(connect, textvariable=self.api_hash, show="●")
        api_hash_entry.pack(fill="x", pady=(4, 6))
        self.credential_entries.append(api_hash_entry)
        ttk.Label(connect, textvariable=self.installation_text, wraplength=540).pack(anchor="w")

        targets = ttk.LabelFrame(connect, text="Автоматически настроить", padding=10)
        targets.pack(fill="x", pady=14)
        for name, title in (
            ("codex", "Codex"),
            ("claude-desktop", "Claude Desktop"),
            ("claude-code", "Claude Code"),
        ):
            ttk.Checkbutton(targets, text=title, variable=self.clients[name]).pack(
                side="left", padx=(0, 20)
            )
        ttk.Checkbutton(
            connect, text="Запускать сервер при входе в Windows", variable=self.autostart
        ).pack(anchor="w")
        self.install_button = self.button(
            connect, "Установить и войти по QR-коду", lambda: self.install("qr")
        )
        self.install_button.pack(fill="x", pady=(10, 8))
        phone_row = ttk.Frame(connect)
        phone_row.pack(fill="x")
        phone_entry = ttk.Entry(phone_row, textvariable=self.phone)
        phone_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.credential_entries.append(phone_entry)
        self.login_button = self.button(
            phone_row, "Войти по номеру", lambda: self.install("phone")
        )
        self.login_button.pack(side="right")
        ttk.Label(
            connect,
            text="Номер телефона с кодом страны нужен только для входа по номеру.",
            wraplength=540,
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(connect, textvariable=self.detail, wraplength=550).pack(anchor="w", pady=8)

        ttk.Label(
            manage, text="Один сервер обслуживает все чаты и все три приложения.", wraplength=530
        ).pack(anchor="w", pady=(0, 14))
        self.start_button = self.button(manage, "Запустить сервер", self.start_server)
        self.start_button.pack(fill="x", pady=5)
        self.stop_button = self.button(manage, "Остановить сервер", self.stop_server)
        self.stop_button.pack(fill="x", pady=5)
        ttk.Label(manage, textvariable=self.detail, wraplength=530).pack(anchor="w", pady=8)
        self.configure_button = self.button(
            manage, "Обновить настройки приложений", self.reconfigure
        )
        self.configure_button.pack(fill="x", pady=5)
        self.button(
            manage, "Открыть папку файлов", lambda: self.open_path(settings.root() / "files")
        ).pack(fill="x", pady=5)
        self.button(
            manage, "Открыть папку журналов", lambda: self.open_path(settings.root() / "logs")
        ).pack(fill="x", pady=5)
        self.button(manage, "Выйти из аккаунта Telegram", self.logout).pack(fill="x", pady=5)
        self.button(manage, "Удалить Telegram MCP", self.uninstall).pack(fill="x", pady=5)
        proxy_tab = ttk.Frame(tabs, padding=16)
        tabs.add(proxy_tab, text="Прокси")
        self.proxy_fields = {}
        for key, title in (
            ("TELEGRAM_PROXY_TYPE", "Тип: socks5, socks4, http или none"),
            ("TELEGRAM_PROXY_HOST", "Адрес"),
            ("TELEGRAM_PROXY_PORT", "Порт"),
            ("TELEGRAM_PROXY_USERNAME", "Имя пользователя"),
            ("TELEGRAM_PROXY_PASSWORD", "Пароль"),
        ):
            self.proxy_fields[key] = tk.StringVar(value=data.get(key, ""))
            ttk.Label(proxy_tab, text=title).pack(anchor="w")
            ttk.Entry(
                proxy_tab,
                textvariable=self.proxy_fields[key],
                show="●" if key.endswith("PASSWORD") else "",
            ).pack(fill="x", pady=(4, 10))
        self.button(proxy_tab, "Сохранить прокси", self.save_proxy).pack(fill="x", pady=10)
        ttk.Label(
            manage, text="API-данные и сеанс Telegram хранятся локально:", wraplength=530
        ).pack(anchor="w", pady=(22, 5))
        ttk.Label(manage, text=str(settings.state()), wraplength=530).pack(anchor="w")
        ttk.Label(
            manage, text="Секреты не записываются в настройки Claude и Codex.", wraplength=530
        ).pack(anchor="w", pady=12)
        ttk.Label(
            outer,
            text="Закрытие этого окна не останавливает сервер. Значок сервера остаётся в области уведомлений.",
            wraplength=620,
        ).pack(anchor="w")
        self.window.after(100, self.poll)
        self.window.after(150, self.refresh_status)
        self.update_buttons()

    def button(self, parent, text, action):
        button = ttk.Button(parent, text=text, command=action)
        self.buttons.append(button)
        return button

    def open_path(self, path):
        import os

        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def ask(self, title, secret=False):
        ready, answer = threading.Event(), []
        self.events.put(("ask", title, secret, ready, answer))
        ready.wait()
        if not answer or answer[0] is None:
            raise RuntimeError("Ввод отменён.")
        return answer[0]

    def selected_clients(self):
        return [name for name, checked in self.clients.items() if checked.get()]

    def update_buttons(self):
        for button in self.buttons:
            button.state(["disabled"] if self.busy else ["!disabled"])
        if self.busy or not self.status_known or self.backend_active:
            self.start_button.state(["disabled"])
        if self.busy or not self.status_known or not self.backend_active:
            self.stop_button.state(["disabled"])
        for button in (self.install_button, self.login_button):
            if self.busy or self.installed:
                button.state(["disabled"])
        if self.busy or not self.installed:
            self.configure_button.state(["disabled"])
        self.install_button.configure(
            text="Установлено" if self.installed else "Установить и войти по QR-коду"
        )
        self.login_button.configure(text="Вход выполнен" if self.installed else "Войти по номеру")
        self.installation_text.set(
            "Установка завершена. Повторная установка не требуется. Для смены аккаунта сначала выполните выход."
            if self.installed
            else "Получите собственные API-данные на my.telegram.org/apps и выполните вход."
        )
        for entry in self.credential_entries:
            entry.state(
                ["disabled"]
                if self.busy
                else ["!disabled", "readonly"] if self.installed else ["!disabled", "!readonly"]
            )
        self.start_button.configure(
            text=(
                "Запуск…"
                if self.service_action == "start"
                else "Сервер запущен" if self.backend_active else "Запустить сервер"
            )
        )
        self.stop_button.configure(
            text=("Остановка…" if self.service_action == "stop" else "Остановить сервер")
        )

    def apply_status(self, result, active=False, installed=None):
        if installed is not None:
            self.installed = installed
        self.last_status = result
        self.backend_active = bool(result) or active
        self.status_known = True
        if result and result.get("connected"):
            text, color = "● Сервер работает. Telegram подключён.", "#1a7f37"
        elif result:
            text, color = "● Сервер запущен. Telegram не подключён.", "#9a6700"
        elif active:
            text, color = "● Процесс сервера запущен. Ожидаю ответа…", "#9a6700"
        else:
            text, color = "○ Сервер остановлен.", "#666666"
        self.state_text.set(text)
        self.status_label.configure(foreground=color)
        self.update_buttons()

    def accept_status(self, generation, result, active, installed=None):
        self.status_pending = False
        if self.busy or generation != self.status_generation:
            return
        initial = not self.status_known
        self.apply_status(result, active, installed)
        if initial and result and result.get("connected"):
            self.detail.set(
                "Сервер готов. Закройте окно и используйте Telegram через Codex или Claude."
            )

    @staticmethod
    def read_status():
        result = service.status()
        return (
            result,
            bool(result) or bool(security.recorded_processes()),
            settings.setup_complete(),
        )

    def start_server(self):
        if not self.status_known or self.backend_active:
            return
        self.work(
            lambda: service.ensure_service(explicit=True),
            "Сервер запущен. Telegram готов к работе.",
            service_action="start",
        )

    def stop_server(self):
        if not self.status_known or not self.backend_active:
            return
        self.work(service.stop_service, "Сервер остановлен.", service_action="stop")

    def work(self, action, success="Готово.", service_action=None):
        if self.busy:
            return
        self.busy = True
        self.status_generation += 1
        self.service_action = service_action
        self.detail.set("Выполняю операцию…")
        if service_action:
            self.state_text.set(
                "Сервер запускается…" if service_action == "start" else "Сервер останавливается…"
            )
            self.status_label.configure(foreground="#9a6700")
        self.progress.start(12)
        self.update_buttons()

        def worker():
            try:
                action()
                self.events.put(("done", success, *self.read_status()))
            except Exception as error:
                if isinstance(error, (ValueError, RuntimeError)):
                    detail = str(error)
                else:
                    detail = f"Операция не завершена ({type(error).__name__}). Проверьте данные и подключение к сети."
                self.events.put(("error", detail, *self.read_status()))

        threading.Thread(target=worker, daemon=True).start()

    def install(self, method):
        if self.busy or self.installed:
            return
        api_id, api_hash, phone = (
            self.api_id.get().strip(),
            self.api_hash.get().strip(),
            self.phone.get().strip(),
        )
        selected, autostart = self.selected_clients(), self.autostart.get()

        def action():
            with service.local_lock("bootstrap.lock", timeout=180):
                service.stop_service()
                settings.save_credentials(api_id, api_hash)
                settings.install_binary()
                try:
                    with service.local_lock("backend.lock", timeout=2):
                        asyncio.run(self.authorize(method, phone))
                    settings.register_task(autostart)
                    settings.configure_clients(selected)
                except Exception:
                    # Leave stopped on failed auth, rather than opening the session twice.
                    raise
            service.ensure_service(explicit=True)

        self.work(
            action,
            "Установка завершена. Перезапустите выбранные приложения, чтобы они загрузили подключение.",
        )

    def reconfigure(self):
        selected = self.selected_clients()
        autostart = self.autostart.get()

        def action():
            settings.register_task(autostart)
            settings.configure_clients(selected)

        self.work(
            action,
            "Настройки сохранены. Перезапустите выбранные приложения.",
        )

    async def authorize(self, method, phone):
        await auth.authorize(method, phone, self.ask, lambda png: self.events.put(("qr", png)))

    def logout(self):
        if not messagebox.askyesno(
            "Telegram MCP", "Завершить сеанс Telegram на этом компьютере?", parent=self.window
        ):
            return

        def action():
            with service.local_lock("bootstrap.lock", timeout=180):
                service.stop_service()
                with service.local_lock("backend.lock", timeout=2):
                    asyncio.run(auth.logout())

        self.work(action, "Сеанс завершён. Для подключения выполните вход заново.")

    def uninstall(self):
        if not messagebox.askyesno(
            "Telegram MCP",
            "Удалить программу, настройки MCP и завершить сеанс? Скачанные файлы сохранятся.",
            parent=self.window,
        ):
            return

        def action():
            import os

            with service.local_lock("bootstrap.lock", timeout=180):
                service.stop_service()
                with service.local_lock("backend.lock", timeout=2):
                    if settings.env_path().exists():
                        asyncio.run(auth.logout())
                settings.unregister()
                security.stop_bridges()
                settings.schedule_binary_removal(os.getpid())
            self.events.put(("exit",))

        self.work(action)

    def save_proxy(self):
        changes = {key: value.get().strip() for key, value in self.proxy_fields.items()}

        def action():
            previous = settings.values()
            settings.save(changes)
            try:
                settings.proxy_config()
            except Exception:
                settings.save({key: previous.get(key, "") for key in changes})
                raise

        self.work(action, "Прокси сохранён. Перезапустите сервер для применения.")

    def poll(self):  # noqa: C901 - single UI event dispatcher
        from PIL import Image, ImageTk

        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "exit":
                    self.window.destroy()
                    return
                if event[0] in {"done", "error"}:
                    self.busy = False
                    self.service_action = None
                    self.progress.stop()
                    self.photo = None
                    if self.qr_window is not None:
                        self.qr_window.destroy()
                        self.qr_window = None
                    self.apply_status(*event[2:])
                    self.detail.set(event[1])
                    if event[0] == "error":
                        messagebox.showerror("Telegram MCP", event[1], parent=self.window)
                elif event[0] == "ask":
                    _, title, secret, ready, answer = event
                    answer.append(
                        simpledialog.askstring(
                            "Telegram MCP", title, show="●" if secret else "", parent=self.window
                        )
                    )
                    ready.set()
                elif event[0] == "qr":
                    if self.qr_window is None or not self.qr_window.winfo_exists():
                        self.qr_window = tk.Toplevel(self.window)
                        self.qr_window.title("Вход в Telegram")
                        self.qr_window.geometry("360x350")
                        self.qr_window.transient(self.window)
                        self.qr_label = ttk.Label(self.qr_window, anchor="center")
                        self.qr_label.pack(pady=14)
                        ttk.Label(
                            self.qr_window,
                            text="На телефоне: Telegram → Настройки → Устройства → Подключить устройство.",
                            wraplength=320,
                        ).pack(padx=15)
                    image = Image.open(io.BytesIO(event[1])).resize((230, 230))
                    self.photo = ImageTk.PhotoImage(image)
                    self.qr_label.configure(image=self.photo)
                    self.detail.set(
                        "На телефоне: Telegram → Настройки → Устройства → Подключить устройство."
                    )
                elif event[0] == "status":
                    self.accept_status(*event[1:])
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def refresh_status(self):
        if not self.status_pending and not self.busy:
            self.status_pending = True
            generation = self.status_generation
            threading.Thread(
                target=lambda: self.events.put(("status", generation, *self.read_status())),
                daemon=True,
            ).start()
        self.window.after(2500, self.refresh_status)

    def run(self):
        self.window.mainloop()

    def close_to_tray(self):
        if self.busy:
            self.detail.set("Дождитесь завершения операции. Затем закройте окно настроек.")
            return
        # The shared service owns the tray icon; closing settings does not stop it.
        self.window.destroy()


def main():
    Manager().run()
