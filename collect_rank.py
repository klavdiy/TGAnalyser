"""Снимок публичных Telegram-каналов из каталога (подписчики + просмотры постов)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, Chat

from tg_auth import ensure_telegram_auth
from rank_store import (
    DEFAULT_CATALOG,
    DEFAULT_DB,
    connect,
    create_snapshot,
    latest_snapshot_id,
    load_catalog,
    replace_channel_snapshot,
    snapshot_done_usernames,
    upsert_catalog,
)
from tg_config import Account, accounts

log = logging.getLogger('collect_rank')

MAX_RETRIES = 3
POSTS_LOOKBACK_DAYS = 14
CHANNEL_DELAY = 0.6
# Короче этого — ждём на том же app. Длиннее — пробуем второй.
SWITCH_AFTER_SEC = 20

RKN_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:'
    r'gosuslugi\.ru/snet/[A-Za-z0-9]+|'
    r'knd\.gov\.ru/license\?[^\s)\'\"<>]+|'
    r'clck\.ru/[A-Za-z0-9]+'
    r')',
    re.IGNORECASE,
)


def extract_rkn_url(*texts: str | None) -> str:
    blob = '\n'.join(t for t in texts if t)
    if not blob:
        return ''
    match = RKN_URL_RE.search(blob)
    if match:
        return match.group(0).rstrip(').,;')
    if re.search(r'РКН|реестр', blob, re.IGNORECASE):
        generic = re.search(r'https?://[^\s)\'\"<>]+', blob)
        if generic:
            return generic.group(0).rstrip(').,;')
    return ''


def count_reactions(msg) -> int:
    total = 0
    if msg.reactions:
        for result in msg.reactions.results:
            total += result.count
    return total


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class AccountPool:
    """Несколько TelegramClient: при длинном FloodWait переключаемся на другой app."""

    def __init__(self, accs: list[Account]):
        self.slots: list[dict] = [
            {'account': a, 'client': None, 'cooldown_until': 0.0} for a in accs
        ]
        self.index = 0

    @property
    def client(self) -> TelegramClient:
        client = self.slots[self.index]['client']
        if client is None:
            raise RuntimeError('пул не запущен')
        return client

    @property
    def label(self) -> str:
        return self.slots[self.index]['account'].name

    async def start(self) -> None:
        for slot in self.slots:
            acc: Account = slot['account']
            log.info('Подключаю app %s (сессия %s)', acc.name, acc.session)
            client = TelegramClient(acc.session, acc.api_id, acc.api_hash)
            await client.connect()
            await ensure_telegram_auth(client)
            slot['client'] = client

    async def close(self) -> None:
        for slot in self.slots:
            client = slot['client']
            if client is not None:
                await client.disconnect()
                slot['client'] = None

    async def handle_flood(self, seconds: int) -> None:
        now = time.monotonic()
        self.slots[self.index]['cooldown_until'] = now + seconds + 1
        log.warning('app %s FloodWait %s сек', self.label, seconds)
        if seconds < SWITCH_AFTER_SEC:
            await asyncio.sleep(seconds + 1)
            return
        for i, slot in enumerate(self.slots):
            if i == self.index:
                continue
            if slot['client'] is not None and slot['cooldown_until'] <= now:
                log.info('переключаюсь на app %s', slot['account'].name)
                self.index = i
                return
        soonest_i = min(
            range(len(self.slots)),
            key=lambda i: self.slots[i]['cooldown_until'],
        )
        wait = max(self.slots[soonest_i]['cooldown_until'] - now, 0)
        log.warning(
            'все app в FloodWait, жду %.0f сек → app %s',
            wait, self.slots[soonest_i]['account'].name,
        )
        await asyncio.sleep(wait + 0.5)
        self.index = soonest_i


async def flood_call(pool: AccountPool, make_coro, retries: int = MAX_RETRIES):
    last_error = None
    for attempt in range(retries + 1):
        try:
            return await make_coro(pool.client)
        except FloodWaitError as exc:
            last_error = exc
            await pool.handle_flood(exc.seconds)
    raise last_error


async def collect_channel(pool: AccountPool, username: str, cutoff: datetime) -> tuple[dict, list[dict]]:
    entity = await flood_call(pool, lambda c, u=username: c.get_entity(u))
    stats: dict = {
        'tg_id': getattr(entity, 'id', None),
        'title': getattr(entity, 'title', None) or getattr(entity, 'first_name', None),
        'about': None,
        'subscribers': None,
        'is_broadcast': None,
        'is_megagroup': None,
        'verified': int(bool(getattr(entity, 'verified', False))),
        'rkn_url': '',
        'last_post_at': None,
        'posts_sampled': 0,
        'error': None,
    }

    if isinstance(entity, Channel):
        stats['is_broadcast'] = int(bool(entity.broadcast))
        stats['is_megagroup'] = int(bool(entity.megagroup))
        full = await flood_call(pool, lambda c, e=entity: c(GetFullChannelRequest(e)))
        stats['subscribers'] = full.full_chat.participants_count
        stats['about'] = full.full_chat.about or ''
        stats['rkn_url'] = extract_rkn_url(stats['about'])
    elif isinstance(entity, Chat):
        stats['is_broadcast'] = 0
        stats['is_megagroup'] = 0
        stats['subscribers'] = getattr(entity, 'participants_count', None)
        stats['about'] = ''
    else:
        stats['error'] = 'not_a_channel'
        return stats, []

    posts: list[dict] = []
    client = pool.client
    async for msg in client.iter_messages(entity, limit=None):
        if msg.date is None:
            continue
        msg_date = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
        if msg_date < cutoff:
            break
        if not msg.post and msg.views is None and not (msg.text or msg.media):
            continue
        posts.append({
            'msg_id': msg.id,
            'posted_at': iso(msg_date),
            'views': msg.views,
            'forwards': msg.forwards or 0,
            'reactions': count_reactions(msg),
        })

    stats['posts_sampled'] = len(posts)
    if posts:
        stats['last_post_at'] = max(p['posted_at'] for p in posts)
    return stats, posts


async def run(args: argparse.Namespace) -> None:
    accs = accounts(args.account)
    log.info('Приложения: %s', ', '.join(f'{a.name}:{a.session}' for a in accs))
    pool = AccountPool(accs)
    await pool.start()
    if args.login:
        log.info('Авторизация ok, сбор не запускаю (--login).')
        await pool.close()
        return

    catalog = load_catalog(Path(args.catalog))
    if args.only_panel:
        catalog = [c for c in catalog if c.get('panel')]
        if not catalog:
            raise SystemExit('В каталоге нет каналов с panel: true')
    if args.limit:
        catalog = catalog[: args.limit]

    conn = connect(Path(args.db))
    ids = upsert_catalog(conn, load_catalog(Path(args.catalog)))

    snapshot_id = None
    done: set[str] = set()
    if args.resume:
        snapshot_id = latest_snapshot_id(conn)
        if snapshot_id is not None:
            done = snapshot_done_usernames(conn, snapshot_id)
            log.info('Продолжаю снимок #%s, уже есть %s каналов', snapshot_id, len(done))
    if snapshot_id is None:
        notes = 'panel' if args.only_panel else 'catalog'
        snapshot_id = create_snapshot(conn, notes=notes)
        log.info('Новый снимок #%s', snapshot_id)

    pending = [c for c in catalog if c['username'].lower() not in done]
    log.info('К сбору: %s из %s', len(pending), len(catalog))

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        log.info('Посты с %s', cutoff.strftime('%Y-%m-%d %H:%M UTC'))

        for i, item in enumerate(pending, 1):
            username = item['username']
            channel_id = ids[username.lower()]
            log.info('[%s/%s] @%s  (app %s)', i, len(pending), username, pool.label)
            try:
                stats, posts = await collect_channel(pool, username, cutoff)
            except FloodWaitError as exc:
                await pool.handle_flood(exc.seconds)
                try:
                    stats, posts = await collect_channel(pool, username, cutoff)
                except Exception as retry_exc:
                    log.warning('@%s ошибка после смены app: %s', username, retry_exc)
                    stats, posts = {'error': str(retry_exc)[:300], 'posts_sampled': 0}, []
            except Exception as exc:
                log.warning('@%s ошибка: %s', username, exc)
                stats, posts = {'error': str(exc)[:300], 'posts_sampled': 0}, []

            replace_channel_snapshot(conn, snapshot_id, channel_id, stats, posts)
            if stats.get('error'):
                log.info('  → ошибка: %s', stats['error'])
            else:
                log.info(
                    '  → подписчики=%s постов=%s broadcast=%s',
                    stats.get('subscribers'),
                    stats.get('posts_sampled'),
                    stats.get('is_broadcast'),
                )
            await asyncio.sleep(args.delay)
    finally:
        await pool.close()
        conn.close()
    log.info('Снимок #%s готов', snapshot_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Снимок каталога Telegram-каналов для рейтинга')
    parser.add_argument('--catalog', default=str(DEFAULT_CATALOG))
    parser.add_argument('--db', default=str(DEFAULT_DB))
    parser.add_argument('--only-panel', action='store_true', help='Только каналы с panel: true')
    parser.add_argument('--resume', action='store_true', help='Дособрать последний снимок')
    parser.add_argument('--limit', type=int, default=0, help='Ограничить число каналов (отладка)')
    parser.add_argument('--days', type=int, default=POSTS_LOOKBACK_DAYS)
    parser.add_argument('--delay', type=float, default=CHANNEL_DELAY)
    parser.add_argument(
        '--account', choices=('auto', '1', '2'), default='auto',
        help='Какое приложение использовать (auto = оба, с переключением)',
    )
    parser.add_argument(
        '--login', action='store_true',
        help='Только QR-авторизация сессий, без сбора',
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)s  %(message)s',
        datefmt='%H:%M:%S',
    )
    asyncio.run(run(parse_args()))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n  Прервано. Запустите снова с --resume.')
        sys.exit(1)
