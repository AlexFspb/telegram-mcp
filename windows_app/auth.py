"""Bounded Telegram login, with an exclusive lock before SQLite is opened."""

import asyncio
from contextlib import contextmanager
import io
import os

from windows_app import VERSION, settings


@contextmanager
def session_lock():
    from telegram_mcp.singleton import SessionLock

    filename = os.path.abspath(settings.state() / "telegram.session")
    lock = SessionLock("default", f"file:{filename}")
    lock.acquire(grace_seconds=0)
    try:
        yield
    finally:
        lock.release()


def client():
    from telethon import TelegramClient

    data = settings.values()
    return TelegramClient(
        str(settings.state() / "telegram"),
        int(data["TELEGRAM_API_ID"]),
        data["TELEGRAM_API_HASH"],
        device_model="Alex Telegram MCP",
        system_version="Windows",
        app_version=VERSION,
        proxy=settings.proxy_config(),
    )


async def password_login(connection, ask):
    from telethon import errors

    for _ in range(5):
        try:
            await connection.sign_in(password=ask("Пароль двухэтапной проверки Telegram", True))
            return
        except errors.PasswordHashInvalidError:
            continue
    raise RuntimeError("Пароль не принят после пяти попыток. Повторите вход позднее.")


async def authorize(method, phone, ask, show_qr):  # noqa: C901
    import qrcode
    from telethon import errors

    with session_lock():
        connection = client()
        try:
            await asyncio.wait_for(connection.connect(), timeout=30)
            if not await connection.is_user_authorized():
                if method == "qr":
                    for _ in range(3):
                        qr = await connection.qr_login()
                        png = io.BytesIO()
                        qrcode.make(qr.url).save(png, format="PNG")
                        show_qr(png.getvalue())
                        try:
                            await qr.wait(timeout=60)
                            break
                        except asyncio.TimeoutError:
                            continue
                        except errors.SessionPasswordNeededError:
                            await password_login(connection, ask)
                            break
                    else:
                        raise RuntimeError("QR-код не подтверждён. Повторите вход.")
                else:
                    if not phone:
                        raise ValueError("Введите номер телефона с кодом страны.")
                    sent = await connection.send_code_request(phone)
                    for _ in range(5):
                        try:
                            await connection.sign_in(
                                phone=phone,
                                code=ask("Код входа из Telegram", True),
                                phone_code_hash=sent.phone_code_hash,
                            )
                            break
                        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                            continue  # Same code request; do not send another SMS/code.
                        except errors.SessionPasswordNeededError:
                            await password_login(connection, ask)
                            break
                        except errors.PhoneCodeExpiredError:
                            raise RuntimeError(
                                "Срок кода истёк. Запросите новый код позднее."
                            ) from None
                    else:
                        raise RuntimeError(
                            "Код не принят после пяти попыток. Повторите вход позднее."
                        )
            if not await connection.is_user_authorized():
                raise RuntimeError("Telegram не подтвердил вход.")
            me = await connection.get_me()
            if me.bot:
                raise ValueError("Для установки нужен личный аккаунт Telegram.")
            settings.save(
                {
                    "TELEGRAM_EXPECTED_USERNAME": me.username or "",
                    "TELEGRAM_EXPECTED_USER_ID": str(me.id),
                }
            )
        except errors.FloodWaitError as error:
            raise RuntimeError(
                f"Telegram ограничил вход. Повторите через {error.seconds} секунд."
            ) from None
        finally:
            await connection.disconnect()


async def logout():
    with session_lock():
        connection = client()
        try:
            await asyncio.wait_for(connection.connect(), timeout=30)
            if await connection.is_user_authorized():
                if not await connection.log_out():
                    raise RuntimeError("Telegram не подтвердил выход. Данные сохранены.")
        finally:
            await connection.disconnect()
    for name in ("telegram.session", "telegram.session-journal", ".env"):
        (settings.state() / name).unlink(missing_ok=True)
