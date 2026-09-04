"""Учётные данные Telegram: одно или два приложения с отдельными сессиями."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SESSION = 'analyser_session'
SESSION_2 = 'analyser_session_2'


def _load_dotenv(path: Path = Path('.env')) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Account:
    name: str
    session: str
    api_id: int
    api_hash: str


def _parse_account(name: str, id_key: str, hash_key: str, session: str) -> Account | None:
    api_id = os.environ.get(id_key, '')
    api_hash = os.environ.get(hash_key, '')
    if not api_id.isdigit() or len(api_hash) < 16:
        return None
    return Account(name=name, session=session, api_id=int(api_id), api_hash=api_hash)


def accounts(which: str = 'auto') -> list[Account]:
    """which: auto | 1 | 2 — auto берёт все заполненные приложения."""
    _load_dotenv()
    first = _parse_account('1', 'TG_API_ID', 'TG_API_HASH', os.environ.get('TG_SESSION', SESSION))
    second = _parse_account(
        '2', 'TG_API_ID_2', 'TG_API_HASH_2', os.environ.get('TG_SESSION_2', SESSION_2),
    )
    if which == '1':
        found = [a for a in (first,) if a]
    elif which == '2':
        found = [a for a in (second,) if a]
    else:
        found = [a for a in (first, second) if a]
    if not found:
        raise SystemExit(
            'Не заданы ключи Telegram.\n'
            'Нужны TG_API_ID / TG_API_HASH, опционально TG_API_ID_2 / TG_API_HASH_2.\n'
            'Скопируйте .env.example в .env.'
        )
    return found


def credentials() -> tuple[int, str]:
    acc = accounts('auto')[0]
    return acc.api_id, acc.api_hash
