"""Загрузка учётных данных Telegram из окружения или .env (без внешних зависимостей)."""

import os
from pathlib import Path

SESSION = 'analyser_session'


def _load_dotenv(path: Path = Path('.env')) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


def credentials() -> tuple[int, str]:
    _load_dotenv()
    api_id = os.environ.get('TG_API_ID', '')
    api_hash = os.environ.get('TG_API_HASH', '')
    if not api_id.isdigit() or len(api_hash) < 16:
        raise SystemExit(
            'Не заданы TG_API_ID / TG_API_HASH.\n'
            'Скопируйте .env.example в .env и впишите ключи с my.telegram.org.'
        )
    return int(api_id), api_hash
