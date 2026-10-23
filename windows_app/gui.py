"""User-facing Windows setup. All interactive input stays on the local machine."""

from __future__ import annotations

import asyncio
import io
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from dotenv import dotenv_values

from windows_app import VERSION, service, settings


class Manager:
    def __init__(self):
        self.window = tk.Tk()
        self.window.title("Telegram MCP — установка и настройки")
        self.window.geometry("680x730")
        self.window.minsize(620, 650)
        self.events = queue.Queue()
        self.busy = False
        self.photo = None
        self.qr_window = None
        self.status_pending = False
        data = settings.values()
        self.api_id = tk.StringVar(value=data.get("TELEGRAM_API_ID", ""))
        self.api_hash = tk.StringVar(value=data.get("TELEGRAM_API_HASH", ""))
        self.phone = tk.StringVar()
        self.state_text = tk.StringVar(value="Проверяю состояние сервера…")
        self.detail = tk.StringVar(value="Заполните API-данные, затем выберите способ входа.")
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
        ttk.Label(outer, textvariable=self.state_text, font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )

        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True, pady=14)
        connect = ttk.Frame(tabs, padding=16)
        manage = ttk.Frame(tabs, padding=16)
        tabs.add(connect, text="Установка и вход")
        tabs.add(manage, text="Сервер и файлы")

        ttk.Label(connect, text="Application API ID").pack(anchor="w")
        ttk.Entry(connect, textvariable=self.api_id).pack(fill="x", pady=(4, 10))
        ttk.Label(connect, text="Application API hash").pack(anchor="w")
        ttk.Entry(connect, textvariable=self.api_hash, show="●").pack(fill="x", pady=(4, 6))
        ttk.Label(
            connect, text="Получите собственные данные на my.telegram.org/apps.", wraplength=540
        ).pack(anchor="w")

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
        self.button(connect, "Установить и войти по QR-коду", lambda: self.install("qr")).pack(
            fill="x", pady=(14, 8)
        )
        phone_row = ttk.Frame(connect)
        phone_row.pack(fill="x")
        ttk.Entry(phone_row, textvariable=self.phone).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        self.button(phone_row, "Войти по номеру", lambda: self.install("phone")).pack(side="right")
        ttk.Label(
            connect,
            text="Номер телефона с кодом страны нужен только для входа по номеру.",
            wraplength=540,
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(connect, textvariable=self.detail, wraplength=550).pack(anchor="w", pady=8)

        ttk.Label(
            manage, text="Один сервер обслуживает все чаты и все три приложения.", wraplength=530
        ).pack(anchor="w", pady=(0, 14))
        for label, action in (
            ("Запустить сервер", service.ensure_service),
            ("Остановить сервер", service.stop_service),
        ):
            self.button(manage, label, lambda action=action: self.work(action)).pack(
                fill="x", pady=5
            )
        self.button(manage, "Обновить настройки приложений", self.reconfigure).pack(
            fill="x", pady=5
        )
        self.button(
            manage, "Открыть папку файлов", lambda: self.open_path(settings.root() / "files")
        ).pack(fill="x", pady=5)
        self.button(
            manage, "Открыть папку журналов", lambda: self.open_path(settings.root() / "logs")
        ).pack(fill="x", pady=5)
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

    def work(self, action, success="Готово."):
        if self.busy:
            return
        self.busy = True
        self.detail.set("Выполняю операцию…")
        for button in self.buttons:
            button.state(["disabled"])

        def worker():
            try:
                action()
                self.events.put(("done", success))
            except Exception as error:
                if isinstance(error, (ValueError, RuntimeError)):
                    detail = str(error)
                else:
                    detail = f"Операция не завершена ({type(error).__name__}). Проверьте данные и подключение к сети."
                self.events.put(("error", detail))

        threading.Thread(target=worker, daemon=True).start()

    def install(self, method):
        api_id, api_hash, phone = (
            self.api_id.get().strip(),
            self.api_hash.get().strip(),
            self.phone.get().strip(),
        )
        selected, autostart = self.selected_clients(), self.autostart.get()

        def action():
            service.stop_service()
            settings.save_credentials(api_id, api_hash)
            settings.install_binary()
            asyncio.run(self.authorize(method, phone))
            settings.register_task(autostart)
            settings.configure_clients(selected)
            service.ensure_service()

        self.work(
            action,
            "Установка завершена. Перезапустите выбранные приложения, чтобы они загрузили подключение.",
        )

    def reconfigure(self):
        selected = self.selected_clients()
        self.work(
            lambda: settings.configure_clients(selected),
            "Настройки сохранены. Перезапустите выбранные приложения.",
        )

    async def authorize(self, method, phone):  # noqa: C901 - bounded interactive auth flow
        import qrcode
        from telethon import TelegramClient, errors

        data = settings.values()
        client = TelegramClient(
            data["TELEGRAM_SESSION_NAME"],
            int(data["TELEGRAM_API_ID"]),
            data["TELEGRAM_API_HASH"],
            device_model="Alex Telegram MCP",
            system_version="Windows",
            app_version=VERSION,
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                if method == "qr":
                    for _ in range(3):
                        qr = await client.qr_login()
                        png = io.BytesIO()
                        qrcode.make(qr.url).save(png, format="PNG")
                        self.events.put(("qr", png.getvalue()))
                        try:
                            await qr.wait(timeout=60)
                            break
                        except asyncio.TimeoutError:
                            continue
                        except errors.SessionPasswordNeededError:
                            await client.sign_in(
                                password=self.ask("Пароль двухэтапной проверки Telegram", True)
                            )
                            break
                    else:
                        raise RuntimeError("QR-код не подтверждён. Повторите вход.")
                else:
                    if not phone:
                        raise ValueError("Введите номер телефона с кодом страны.")
                    sent = await client.send_code_request(phone)
                    try:
                        await client.sign_in(
                            phone=phone,
                            code=self.ask("Код входа из Telegram", True),
                            phone_code_hash=sent.phone_code_hash,
                        )
                    except errors.SessionPasswordNeededError:
                        await client.sign_in(
                            password=self.ask("Пароль двухэтапной проверки Telegram", True)
                        )
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram не подтвердил вход.")
            me = await client.get_me()
            if me.bot:
                raise ValueError("Для установки нужен личный аккаунт Telegram.")
            settings.save({"TELEGRAM_EXPECTED_USERNAME": me.username or ""})
        finally:
            await client.disconnect()

    def poll(self):  # noqa: C901 - single UI event dispatcher
        from PIL import Image, ImageTk

        try:
            while True:
                event = self.events.get_nowait()
                if event[0] in {"done", "error"}:
                    self.busy = False
                    self.photo = None
                    if self.qr_window is not None:
                        self.qr_window.destroy()
                        self.qr_window = None
                    for button in self.buttons:
                        button.state(["!disabled"])
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
                    self.status_pending = False
                    result = event[1]
                    self.state_text.set(
                        f"● Сервер работает · PID {result['pid']} · 127.0.0.1:{settings.port()}"
                        if result and result.get("connected")
                        else "○ Сервер остановлен или Telegram не подключён"
                    )
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def refresh_status(self):
        if not self.status_pending:
            self.status_pending = True
            threading.Thread(
                target=lambda: self.events.put(("status", service.status())), daemon=True
            ).start()
        self.window.after(2500, self.refresh_status)

    def run(self):
        self.window.mainloop()


def main():
    Manager().run()
