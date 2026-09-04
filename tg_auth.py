"""QR-вход в Telegram. Пароль облака — только из окружения, не из репозитория."""

from __future__ import annotations

import logging
import os

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from tg_config import _load_dotenv

log = logging.getLogger('tg_auth')

QR_TIMEOUT = 600

QR_AUTH_INSTRUCTIONS = """
Telegram не присылает SMS — вход через приложение.

На телефоне: Настройки → Устройства → Подключить устройство → QR.
На компьютере: Telegram Desktop → Настройки → Устройства → камера.
После подтверждения сессия сохранится локально.
"""


def _print_qr(url: str) -> None:
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        print('  (для QR в терминале: pip install qrcode)')
        print(f'  Ссылка: {url}')


def _cloud_password() -> str:
    _load_dotenv()
    env_pwd = os.environ.get('TG_CLOUD_PASSWORD', '').strip()
    if env_pwd:
        return env_pwd
    raise SystemExit(
        'Включён облачный пароль Telegram.\n'
        'Задайте TG_CLOUD_PASSWORD в .env и повторите --login.'
    )


async def ensure_telegram_auth(client: TelegramClient) -> None:
    if await client.is_user_authorized():
        me = await client.get_me()
        name = me.first_name or me.username or 'ok'
        log.info('Сессия уже авторизована (%s)', name)
        return

    print(QR_AUTH_INSTRUCTIONS)
    qr = await client.qr_login()
    print(f'  Ссылка: {qr.url}\n')
    _print_qr(qr.url)
    print(f'\n  Ожидаю подтверждение (до {QR_TIMEOUT // 60} мин)...')
    try:
        await qr.wait(timeout=QR_TIMEOUT)
    except SessionPasswordNeededError:
        await client.sign_in(password=_cloud_password())
        log.info('Облачный пароль принят.')
    me = await client.get_me()
    name = me.first_name or me.username or 'ok'
    log.info('Авторизация успешна (%s)', name)
