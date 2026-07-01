import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from tqdm import tqdm
from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument,
    MessageMediaWebPage, MessageMediaPoll,
    DocumentAttributeVideo, DocumentAttributeAudio,
)
from telethon.errors import FloodWaitError

SESSION_FILE  = 'analyser_session'
MAX_RETRIES   = 3
TEXT_PREVIEW  = 300
QR_TIMEOUT    = 600  # секунд на сканирование QR

TG_LINK_RE = re.compile(
    r'(https?://t\.me/[^\s\)\]\"\'<>]+|@[A-Za-z0-9_]{5,})',
    re.IGNORECASE
)

QR_AUTH_INSTRUCTIONS = """
╔══════════════════════════════════════════════════════════════╗
║              АВТОРИЗАЦИЯ ЧЕРЕЗ QR-КОД                        ║
╠══════════════════════════════════════════════════════════════╣
║  Telegram не присылает SMS — вход только через приложение.   ║
║                                                              ║
║  На телефоне (где уже открыт ваш аккаунт Telegram):          ║
║    1. Откройте Telegram                                      ║
║    2. Настройки → Устройства → Подключить устройство         ║
║    3. Отсканируйте QR-код ниже                               ║
║                                                              ║
║  На компьютере (Telegram Desktop):                           ║
║    Настройки → Устройства → Подключить устройство → камера   ║
║                                                              ║
║  После сканирования подтвердите вход на телефоне.            ║
║  Сессия сохранится — повторная авторизация не понадобится.   ║
╚══════════════════════════════════════════════════════════════╝
"""


@dataclass
class RunConfig:
    api_id: int
    api_hash: str
    channel: str
    days_back: int
    output_dir: Path
    tag: str | None = None


def make_output_dirs(output_base: Path, channel: str) -> dict:
    """Создаёт структуру папок и возвращает пути."""
    base = output_base / channel
    dirs = {
        'base':    base,
        'chats':   base / 'chats',
        'links':   base / 'crawled_links',
        'members': base / 'members',
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)


# ── Псевдо-GUI: мастер настройки ─────────────────────────────────────────────

def _banner(title: str):
    width = 62
    print()
    print('═' * width)
    print(f'  {title}')
    print('═' * width)


def _step(n: int, total: int, text: str):
    print(f'\n── Шаг {n}/{total}: {text} ──')


def _ask(prompt: str, default: str = '') -> str:
    hint = f' [{default}]' if default else ''
    value = input(f'  {prompt}{hint}: ').strip()
    return value or default


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = 'Y/n' if default else 'y/N'
    value = input(f'  {prompt} ({suffix}): ').strip().lower()
    if not value:
        return default
    return value in ('y', 'yes', 'д', 'да')


def _ask_api_id() -> int:
    while True:
        raw = _ask('API ID (число с my.telegram.org)')
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print('  ✗ Введите положительное число.')


def _ask_api_hash() -> str:
    while True:
        raw = _ask('API Hash (строка с my.telegram.org)')
        if len(raw) >= 16:
            return raw
        print('  ✗ Hash слишком короткий — проверьте значение на my.telegram.org')


def _ask_channel() -> str:
    while True:
        raw = _ask('Канал (username без @, например: mychannel)')
        channel = raw.lstrip('@').strip()
        if channel and re.fullmatch(r'[A-Za-z0-9_]{3,}', channel):
            return channel
        print('  ✗ Укажите корректный username канала (латиница, цифры, _).')


def _ask_days_back() -> int:
    print('  0 — вся доступная история канала')
    while True:
        raw = _ask('Срок сбора (дней назад)', '30')
        if raw.isdigit():
            return int(raw)
        print('  ✗ Введите число (0 или больше).')


def _ask_output_dir() -> Path:
    raw = _ask('Папка экспорта', 'TGSpyder_Output')
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _normalize_tag(tag: str) -> str:
    return tag.lstrip('#').strip().lower()


def _ask_tag() -> str | None:
    print('  Пусто — собрать все сообщения без фильтра по тегу')
    raw = _ask('Хэштег для фильтра (# необязателен)', '')
    if not raw:
        return None
    tag = _normalize_tag(raw)
    if not tag:
        return None
    if not re.fullmatch(r'[\w\u0400-\u04FF]+', tag, re.UNICODE):
        print('  ✗ Некорректный хэштег — используйте буквы, цифры и _')
        return _ask_tag()
    return tag


def _safe_tag_filename(tag: str) -> str:
    return re.sub(r'[^\w\-]', '_', _normalize_tag(tag))


def run_wizard() -> RunConfig:
    _banner('TGAnalyser — мастер настройки')
    print('  Ключи API: https://my.telegram.org → API development tools → Desktop')

    _step(1, 6, 'API ID')
    api_id = _ask_api_id()

    _step(2, 6, 'API Hash')
    api_hash = _ask_api_hash()

    _step(3, 6, 'Канал')
    channel = _ask_channel()

    _step(4, 6, 'Срок сбора')
    days_back = _ask_days_back()

    _step(5, 6, 'Фильтр по хэштегу')
    tag = _ask_tag()

    _step(6, 6, 'Папка экспорта')
    output_dir = _ask_output_dir()

    return RunConfig(
        api_id=api_id,
        api_hash=api_hash,
        channel=channel,
        days_back=days_back,
        output_dir=output_dir,
        tag=tag,
    )


def _period_label(days_back: int) -> str:
    if days_back == 0:
        return 'вся история'
    since = datetime.now() - timedelta(days=days_back)
    return f'последние {days_back} дн. (с {since.strftime("%Y-%m-%d")})'


def print_summary(cfg: RunConfig):
    _banner('Проверьте настройки перед запуском')
    print(f'  API ID        : {cfg.api_id}')
    print(f'  API Hash      : {cfg.api_hash[:6]}…{cfg.api_hash[-4:]}')
    print(f'  Канал         : @{cfg.channel}')
    print(f'  Срок сбора    : {_period_label(cfg.days_back)}')
    tag_label = f'#{cfg.tag}' if cfg.tag else 'нет (все сообщения)'
    print(f'  Фильтр по тегу: {tag_label}')
    print(f'  Папка экспорта: {cfg.output_dir / cfg.channel}')
    print(f'  Сессия        : {Path(SESSION_FILE).resolve()}')
    print('═' * 62)


def confirm_run(cfg: RunConfig) -> bool:
    print_summary(cfg)
    return _ask_yes_no('Запустить сбор?', default=True)


# ── QR-авторизация ─────────────────────────────────────────────────────────────

def _print_qr(url: str):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        print('  (для QR в терминале: pip install qrcode)')
        print(f'  Ссылка: {url}')


async def ensure_telegram_auth(client: TelegramClient):
    if await client.is_user_authorized():
        me = await client.get_me()
        name = me.first_name or me.username or 'пользователь'
        log.info(f'Уже авторизован: {name} ({me.phone or "без номера"})')
        return

    print(QR_AUTH_INSTRUCTIONS)
    qr = await client.qr_login()
    print(f'  Ссылка для ручного открытия: {qr.url}\n')
    _print_qr(qr.url)
    print(f'\n  Ожидаю подтверждение (до {QR_TIMEOUT // 60} мин)...')
    await qr.wait(timeout=QR_TIMEOUT)
    me = await client.get_me()
    log.info(f'Авторизация успешна: {me.first_name} ({me.phone})')


# ── Сбор и аналитика ─────────────────────────────────────────────────────────

def detect_media_type(msg) -> str:
    if msg.media is None:
        return 'text'
    if isinstance(msg.media, MessageMediaPhoto):
        return 'photo'
    if isinstance(msg.media, MessageMediaDocument):
        for attr in msg.media.document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return 'video'
            if isinstance(attr, DocumentAttributeAudio):
                return 'audio'
        return 'document'
    if isinstance(msg.media, MessageMediaWebPage):
        return 'link_preview'
    if isinstance(msg.media, MessageMediaPoll):
        return 'poll'
    return 'other_media'


def count_reactions(msg):
    breakdown, total = {}, 0
    if msg.reactions:
        for r in msg.reactions.results:
            emoji = getattr(r.reaction, 'emoticon', '?')
            breakdown[emoji] = r.count
            total += r.count
    return total, breakdown


def extract_entities(msg) -> dict:
    mentions = hashtags = urls = 0
    if msg.entities:
        from telethon.tl.types import (
            MessageEntityMention, MessageEntityHashtag,
            MessageEntityUrl, MessageEntityTextUrl,
        )
        for e in msg.entities:
            if isinstance(e, MessageEntityMention):
                mentions += 1
            elif isinstance(e, MessageEntityHashtag):
                hashtags += 1
            elif isinstance(e, (MessageEntityUrl, MessageEntityTextUrl)):
                urls += 1
    return {'mentions': mentions, 'hashtags': hashtags, 'urls': urls}


def extract_tg_links(msg) -> list[str]:
    if not msg.text:
        return []
    return list(dict.fromkeys(TG_LINK_RE.findall(msg.text)))


def message_has_tag(msg, tag: str) -> bool:
    """Проверяет, содержит ли сообщение указанный хэштег."""
    needle = _normalize_tag(tag)
    if not needle or not msg.text:
        return False

    if msg.entities:
        from telethon.tl.types import MessageEntityHashtag
        for e in msg.entities:
            if isinstance(e, MessageEntityHashtag):
                hashtag = msg.text[e.offset:e.offset + e.length].lstrip('#').lower()
                if hashtag == needle:
                    return True

    return any(
        m.group(1).lower() == needle
        for m in re.finditer(r'#([\w\u0400-\u04FF]+)', msg.text, re.UNICODE)
    )


async def fetch_members(client, entity) -> list[dict]:
    members = []
    try:
        from telethon.tl.functions.channels import GetParticipantsRequest
        from telethon.tl.types import ChannelParticipantsSearch

        offset, limit = 0, 200
        pbar = tqdm(desc='Участники', unit='user')

        while True:
            result = await client(GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsSearch(''),
                offset=offset,
                limit=limit,
                hash=0,
            ))
            if not result.users:
                break
            for u in result.users:
                members.append({
                    'ID':            u.id,
                    'Username':      u.username or '',
                    'Имя':           (u.first_name or '') + ' ' + (u.last_name or ''),
                    'Бот':           u.bot,
                    'Верифицирован': u.verified,
                    'Телефон':       u.phone or '',
                })
            offset += len(result.users)
            pbar.update(len(result.users))
            if offset >= result.count:
                break
        pbar.close()
        log.info(f'Участников собрано: {len(members)}')
    except Exception as ex:
        log.warning(f'Участников получить не удалось (это нормально для публичных каналов): {ex}')
    return members


async def fetch_all(
    client, channel: str, days_back: int, tag: str | None = None,
) -> tuple[list[dict], list[dict], object]:
    entity = await client.get_entity(channel)
    cutoff = None
    if days_back > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        log.info(
            f'Канал: {channel} — выгрузка постов с '
            f'{cutoff.astimezone().strftime("%Y-%m-%d %H:%M")} ...'
        )
    else:
        log.info(f'Канал: {channel} — выгрузка всей истории ...')

    if tag:
        log.info(f'Фильтр по хэштегу: #{_normalize_tag(tag)}')

    records, links_records, retries = [], [], 0
    seen_links: set[str] = set()
    scanned = 0

    pbar = tqdm(unit='msg', desc='Сканирование')

    async for msg in client.iter_messages(entity, limit=None):
        try:
            if cutoff and msg.date < cutoff:
                break

            if not msg.text and not msg.media:
                continue

            scanned += 1
            if tag and not message_has_tag(msg, tag):
                pbar.update(1)
                continue

            total_react, react_breakdown = count_reactions(msg)
            ent   = extract_entities(msg)
            mtype = detect_media_type(msg)

            text_full    = msg.text or ''
            text_preview = (
                text_full[:TEXT_PREVIEW].replace('\n', ' ')
                if text_full else f'[{mtype.upper()}]'
            )

            dt_local = msg.date.replace(tzinfo=timezone.utc).astimezone()

            fwd_from_channel = ''
            fwd_from_post_id = None
            if msg.fwd_from:
                fwd = msg.fwd_from
                if hasattr(fwd, 'from_id') and fwd.from_id:
                    fwd_from_channel = str(fwd.from_id)
                if hasattr(fwd, 'channel_post'):
                    fwd_from_post_id = fwd.channel_post

            records.append({
                'ID':                msg.id,
                'Дата':              dt_local.strftime('%Y-%m-%d %H:%M:%S'),
                'День_недели':       dt_local.strftime('%A'),
                'Час':               dt_local.hour,
                'Тип_медиа':         mtype,
                'Текст':             text_preview,
                'Текст_полный':      text_full,
                'Длина_текста':      len(text_full),
                'Есть_ссылки':       ent['urls'] > 0,
                'Упоминания':        ent['mentions'],
                'Хэштеги':           ent['hashtags'],
                'URL_в_тексте':      ent['urls'],
                'Просмотры':         msg.views or 0,
                'Репосты':           msg.forwards or 0,
                'Реакции':           total_react,
                'Комментарии':       msg.replies.replies if msg.replies else 0,
                'Реакции_детали':    json.dumps(react_breakdown, ensure_ascii=False),
                'Engagement_%':      round(total_react / msg.views * 100, 2) if msg.views else 0,
                'Score':             round(
                    (msg.views or 0) * 0.5
                    + total_react * 10
                    + (msg.forwards or 0) * 20
                ),
                'Закреплён':         msg.pinned,
                'Редактировался':    bool(msg.edit_date),
                'Переслан_из':       fwd_from_channel,
                'Переслан_пост_ID':  fwd_from_post_id,
                'TG_ссылок_в_посте': len(extract_tg_links(msg)),
            })

            for link in extract_tg_links(msg):
                if link not in seen_links:
                    seen_links.add(link)
                    links_records.append({
                        'Ссылка':    link,
                        'Пост_ID':   msg.id,
                        'Дата':      dt_local.strftime('%Y-%m-%d %H:%M:%S'),
                        'Просмотры': msg.views or 0,
                        'Реакции':   total_react,
                    })

            pbar.update(1)

        except FloodWaitError as e:
            if retries >= MAX_RETRIES:
                log.error('FloodWait: превышено число попыток, сохраняю собранное.')
                break
            log.warning(f'FloodWait: жду {e.seconds} сек...')
            await asyncio.sleep(e.seconds + 1)
            retries += 1
        except Exception as ex:
            log.warning(f'Ошибка на msg.id={msg.id}: {ex}')

    pbar.close()
    if tag:
        log.info(
            f'Просмотрено: {scanned}, с тегом #{_normalize_tag(tag)}: {len(records)}, '
            f'уникальных TG-ссылок: {len(links_records)}'
        )
    else:
        log.info(f'Собрано постов: {len(records)}, уникальных TG-ссылок: {len(links_records)}')
    return records, links_records, entity


def build_excel(df: pd.DataFrame, df_links: pd.DataFrame, df_members: pd.DataFrame, path: Path):
    log.info(f'Формирую Excel → {path}')
    df['Дата']  = pd.to_datetime(df['Дата'])
    df['Месяц'] = df['Дата'].dt.to_period('M').astype(str)

    agg_month = (
        df.groupby('Месяц').agg(
            Постов         =('ID',            'count'),
            Avg_Просмотры  =('Просмотры',     'mean'),
            Avg_Реакции    =('Реакции',       'mean'),
            Avg_Репосты    =('Репосты',       'mean'),
            Avg_Комменты   =('Комментарии',   'mean'),
            Avg_Engagement =('Engagement_%',  'mean'),
            Сумма_Score    =('Score',         'sum'),
        ).round(2).reset_index()
    )

    agg_dow = (
        df.groupby('День_недели').agg(
            Постов         =('ID',            'count'),
            Avg_Просмотры  =('Просмотры',     'mean'),
            Avg_Реакции    =('Реакции',       'mean'),
            Avg_Репосты    =('Репосты',       'mean'),
            Avg_Engagement =('Engagement_%',  'mean'),
        ).round(2).reset_index()
    )

    agg_hour = (
        df.groupby('Час').agg(
            Постов         =('ID',            'count'),
            Avg_Просмотры  =('Просмотры',     'mean'),
            Avg_Реакции    =('Реакции',       'mean'),
            Avg_Engagement =('Engagement_%',  'mean'),
        ).round(2).reset_index()
        .sort_values('Avg_Просмотры', ascending=False)
    )

    top_posts = df.nlargest(20, 'Score')[[
        'ID', 'Дата', 'Тип_медиа', 'Текст',
        'Просмотры', 'Реакции', 'Репосты', 'Комментарии',
        'Engagement_%', 'Score',
    ]]

    top_links = pd.DataFrame()
    if not df_links.empty:
        top_links = (
            df_links.groupby('Ссылка')
            .agg(
                Упоминаний     =('Пост_ID', 'count'),
                Первый_пост    =('Дата',    'min'),
                Последний_пост =('Дата',    'max'),
            )
            .sort_values('Упоминаний', ascending=False)
            .reset_index()
        )

    fwd_df = (
        df[df['Переслан_из'] != ''][
            ['ID', 'Дата', 'Переслан_из', 'Переслан_пост_ID', 'Просмотры', 'Реакции']
        ].copy()
        if 'Переслан_из' in df.columns else pd.DataFrame()
    )

    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Raw data', index=False)
        agg_month.to_excel(writer, sheet_name='By Month', index=False)
        agg_dow.to_excel(writer, sheet_name='By Weekday', index=False)
        agg_hour.to_excel(writer, sheet_name='By Hour', index=False)
        top_posts.to_excel(writer, sheet_name='Top Posts', index=False)
        if not df_links.empty:
            df_links.to_excel(writer, sheet_name='All Links', index=False)
            top_links.to_excel(writer, sheet_name='Top Links', index=False)
        if not fwd_df.empty:
            fwd_df.to_excel(writer, sheet_name='Forwards', index=False)
        if not df_members.empty:
            df_members.to_excel(writer, sheet_name='Members', index=False)

        for sheet in writer.sheets.values():
            for col in sheet.columns:
                width = max(
                    (len(str(c.value)) for c in col if c.value),
                    default=10,
                )
                sheet.column_dimensions[col[0].column_letter].width = min(width + 2, 60)

    log.info('Excel готов.')


async def run_collection(cfg: RunConfig):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dirs = make_output_dirs(cfg.output_dir, cfg.channel)
    log.info(f'Папка вывода: {dirs["base"]}')

    client = TelegramClient(SESSION_FILE, cfg.api_id, cfg.api_hash)
    await client.connect()
    try:
        await ensure_telegram_auth(client)
        records, links_records, entity = await fetch_all(
            client, cfg.channel, cfg.days_back, cfg.tag,
        )
        members = await fetch_members(client, entity)
    finally:
        await client.disconnect()

    if not records:
        log.error('Ничего не собрано — файл не создан.')
        return

    channel = cfg.channel
    tag_suffix = f'_{_safe_tag_filename(cfg.tag)}' if cfg.tag else ''
    df         = pd.DataFrame(records).sort_values('ID', ascending=False)
    df_links   = pd.DataFrame(links_records)
    df_members = pd.DataFrame(members)

    chats_csv   = dirs['chats']   / f'messages_{channel}{tag_suffix}_{timestamp}.csv'
    links_csv   = dirs['links']   / f'crawled_links_{channel}{tag_suffix}_{timestamp}.csv'
    members_csv = dirs['members'] / f'members_{channel}{tag_suffix}_{timestamp}.csv'

    df.to_csv(chats_csv, index=False, encoding='utf-8-sig')
    log.info(f'CSV постов    → {chats_csv}')

    if not df_links.empty:
        df_links.to_csv(links_csv, index=False, encoding='utf-8-sig')
        log.info(f'CSV ссылок    → {links_csv}')

    if not df_members.empty:
        df_members.to_csv(members_csv, index=False, encoding='utf-8-sig')
        log.info(f'CSV участников→ {members_csv}')

    xlsx_path = dirs['base'] / f'channel_stats_{channel}{tag_suffix}_{timestamp}.xlsx'
    build_excel(df, df_links, df_members, xlsx_path)

    log.info('─' * 50)
    log.info(f'Строк в файле  : {len(df)}')
    log.info(f'Период         : {df["Дата"].min()} → {df["Дата"].max()}')
    log.info(f'Avg Engagement : {df["Engagement_%"].mean():.2f}%')
    log.info(f'Лучший пост    : ID={df.loc[df["Score"].idxmax(), "ID"]}  Score={df["Score"].max()}')
    log.info(f'TG-ссылок найдено: {len(df_links)}')
    log.info(f'Участников     : {len(df_members)}')
    log.info(f'Файл           : {xlsx_path}')


async def main():
    cfg = run_wizard()
    if not confirm_run(cfg):
        print('\n  Отменено.')
        return
    print('\n  Запуск...\n')
    await run_collection(cfg)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n  Прервано пользователем.')
        sys.exit(1)
