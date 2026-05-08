import asyncio
import json
import logging
import re
from datetime import timezone
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

API_ID        = 30631552                            # ← api_id
API_HASH      = '01425afc8289e8f837d1711a0867aee5'  # ← api_hash
CHANNEL       = 'cyberpoleshuk'                     # ← без @

SESSION_FILE  = 'analyser_session'
MAX_RETRIES   = 3                                   # сколько раз подряд пытаться при FloodWait, прежде чем сдаться и сохранить то, что есть
TEXT_PREVIEW  = 300                                 # сколько символов сохранять в превью (для удобства анализа в Excel)

TG_LINK_RE = re.compile(
    r'(https?://t\.me/[^\s\)\]\"\'<>]+|@[A-Za-z0-9_]{5,})',
    re.IGNORECASE
)

def make_output_dirs(channel: str) -> dict:
    """Создаёт структуру папок и возвращает пути."""
    base = Path('TGSpyder_Output') / channel
    dirs = {
        'base':   base,
        'chats':  base / 'chats',
        'links':  base / 'crawled_links',
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
    """Вытаскивает все t.me-ссылки и @упоминания из текста поста."""
    if not msg.text:
        return []
    return list(dict.fromkeys(TG_LINK_RE.findall(msg.text)))  # уникальные, порядок сохранён

async def fetch_members(client, entity) -> list[dict]:
    """
    Пытается собрать список участников.
    Работает для групп/супергрупп. Для публичных каналов
    Telegram API возвращает пустой список — это нормально.
    """
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
                    'ID':          u.id,
                    'Username':    u.username or '',
                    'Имя':         (u.first_name or '') + ' ' + (u.last_name or ''),
                    'Бот':         u.bot,
                    'Верифицирован': u.verified,
                    'Телефон':     u.phone or '',
                })
            offset += len(result.users)
            pbar.update(len(result.users))
            if offset >= result.count:
                break
        pbar.close()
        log.info(f"Участников собрано: {len(members)}")
    except Exception as ex:
        log.warning(f"Участников получить не удалось (это нормально для публичных каналов): {ex}")
    return members

async def fetch_all(client, channel) -> tuple[list[dict], list[dict]]:
    """
    Возвращает кортеж: (records, links_records)
    records      — все посты с метриками
    links_records — все найденные TG-ссылки с привязкой к посту
    """
    entity = await client.get_entity(channel)
    total  = (await client.get_messages(entity, limit=1)).total
    log.info(f"Сообщений в канале: {total} — начинаю выгрузку...")

    records, links_records, retries = [], [], 0
    seen_links: set[str] = set()   # дедупликация ссылок по всей истории

    pbar = tqdm(total=total, unit='msg', desc='Выгрузка')

    async for msg in client.iter_messages(entity, limit=None):
        try:
            if not msg.text and not msg.media:
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
                'ID':               msg.id,
                'Дата':             dt_local.strftime('%Y-%m-%d %H:%M:%S'),
                'День_недели':      dt_local.strftime('%A'),
                'Час':              dt_local.hour,
                'Тип_медиа':        mtype,
                'Текст':            text_preview,
                'Длина_текста':     len(text_full),
                'Есть_ссылки':      ent['urls'] > 0,
                'Упоминания':       ent['mentions'],
                'Хэштеги':          ent['hashtags'],
                'URL_в_тексте':     ent['urls'],
                'Просмотры':        msg.views    or 0,
                'Репосты':          msg.forwards or 0,
                'Реакции':          total_react,
                'Комментарии':      msg.replies.replies if msg.replies else 0,
                'Реакции_детали':   json.dumps(react_breakdown, ensure_ascii=False),
                'Engagement_%':     round(total_react / msg.views * 100, 2) if msg.views else 0,
                'Score':            round(
                                        (msg.views or 0) * 0.5
                                        + total_react * 10
                                        + (msg.forwards or 0) * 20
                                    ),
                'Закреплён':        msg.pinned,
                'Редактировался':   bool(msg.edit_date),
                # ── новые поля ──────────────────────────────────────
                'Переслан_из':      fwd_from_channel,
                'Переслан_пост_ID': fwd_from_post_id,
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
                log.error("FloodWait: превышено число попыток, сохраняю собранное.")
                break
            log.warning(f"FloodWait: жду {e.seconds} сек...")
            await asyncio.sleep(e.seconds + 1)
            retries += 1
        except Exception as ex:
            log.warning(f"Ошибка на msg.id={msg.id}: {ex}")

    pbar.close()
    log.info(f"Собрано постов: {len(records)}, уникальных TG-ссылок: {len(links_records)}")
    return records, links_records, entity

def build_excel(df: pd.DataFrame, df_links: pd.DataFrame, df_members: pd.DataFrame, path: Path):
    log.info(f"Формирую Excel → {path}")
    df['Дата']  = pd.to_datetime(df['Дата'])
    df['Месяц'] = df['Дата'].dt.to_period('M').astype(str)

    agg_month = (
        df.groupby('Месяц').agg(
            Постов        =('ID',           'count'),
            Avg_Просмотры =('Просмотры',    'mean'),
            Avg_Реакции   =('Реакции',      'mean'),
            Avg_Репосты   =('Репосты',      'mean'),
            Avg_Комменты  =('Комментарии',  'mean'),
            Avg_Engagement=('Engagement_%', 'mean'),
            Сумма_Score   =('Score',        'sum'),
        ).round(2).reset_index()
    )

    agg_dow = (
        df.groupby('День_недели').agg(
            Постов        =('ID',           'count'),
            Avg_Просмотры =('Просмотры',    'mean'),
            Avg_Реакции   =('Реакции',      'mean'),
            Avg_Репосты   =('Репосты',      'mean'),
            Avg_Engagement=('Engagement_%', 'mean'),
        ).round(2).reset_index()
    )

    agg_hour = (
        df.groupby('Час').agg(
            Постов        =('ID',           'count'),
            Avg_Просмотры =('Просмотры',    'mean'),
            Avg_Реакции   =('Реакции',      'mean'),
            Avg_Engagement=('Engagement_%', 'mean'),
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
                Упоминаний   =('Пост_ID',   'count'),
                Первый_пост  =('Дата',      'min'),
                Последний_пост=('Дата',     'max'),
            )
            .sort_values('Упоминаний', ascending=False)
            .reset_index()
        )

    fwd_df = df[df['Переслан_из'] != ''][['ID', 'Дата', 'Переслан_из', 'Переслан_пост_ID', 'Просмотры', 'Реакции']].copy() if 'Переслан_из' in df.columns else pd.DataFrame()

    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        df.to_excel(         writer, sheet_name='Raw data',    index=False)
        agg_month.to_excel(  writer, sheet_name='By Month',    index=False)
        agg_dow.to_excel(    writer, sheet_name='By Weekday',  index=False)
        agg_hour.to_excel(   writer, sheet_name='By Hour',     index=False)
        top_posts.to_excel(  writer, sheet_name='Top Posts',   index=False)
        if not df_links.empty:
            df_links.to_excel(   writer, sheet_name='All Links',   index=False)
            top_links.to_excel(  writer, sheet_name='Top Links',   index=False)
        if not fwd_df.empty:
            fwd_df.to_excel(     writer, sheet_name='Forwards',    index=False)
        if not df_members.empty:
            df_members.to_excel( writer, sheet_name='Members',     index=False)

        for sheet in writer.sheets.values():
            for col in sheet.columns:
                width = max(
                    (len(str(c.value)) for c in col if c.value),
                    default=10
                )
                sheet.column_dimensions[col[0].column_letter].width = min(width + 2, 60)

    log.info("Excel готов.")

async def main():
    from datetime import datetime
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    dirs = make_output_dirs(CHANNEL)
    log.info(f"Папка вывода: {dirs['base']}")

    async with TelegramClient(SESSION_FILE, API_ID, API_HASH) as client:
        records, links_records, entity = await fetch_all(client, CHANNEL)
        members = await fetch_members(client, entity)

    if not records:
        log.error("Ничего не собрано — файл не создан.")
        return

    df         = pd.DataFrame(records).sort_values('ID', ascending=False)
    df_links   = pd.DataFrame(links_records)
    df_members = pd.DataFrame(members)

    chats_csv   = dirs['chats']   / f'messages_{CHANNEL}_{timestamp}.csv'
    links_csv   = dirs['links']   / f'crawled_links_{CHANNEL}_{timestamp}.csv'
    members_csv = dirs['members'] / f'members_{CHANNEL}_{timestamp}.csv'

    df.to_csv(chats_csv, index=False, encoding='utf-8-sig')
    log.info(f"CSV постов    → {chats_csv}")

    if not df_links.empty:
        df_links.to_csv(links_csv, index=False, encoding='utf-8-sig')
        log.info(f"CSV ссылок    → {links_csv}")

    if not df_members.empty:
        df_members.to_csv(members_csv, index=False, encoding='utf-8-sig')
        log.info(f"CSV участников→ {members_csv}")

    xlsx_path = dirs['base'] / f'channel_stats_{CHANNEL}_{timestamp}.xlsx'
    build_excel(df, df_links, df_members, xlsx_path)

    log.info("─" * 50)
    log.info(f"Строк в файле  : {len(df)}")
    log.info(f"Период         : {df['Дата'].min()} → {df['Дата'].max()}")
    log.info(f"Avg Engagement : {df['Engagement_%'].mean():.2f}%")
    log.info(f"Лучший пост    : ID={df.loc[df['Score'].idxmax(), 'ID']}  Score={df['Score'].max()}")
    log.info(f"TG-ссылок найдено: {len(df_links)}")
    log.info(f"Участников     : {len(df_members)}")
    log.info(f"Файл           : {xlsx_path}")


if __name__ == '__main__':
    asyncio.run(main())