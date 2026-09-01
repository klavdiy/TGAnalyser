import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient

from tg_config import SESSION, credentials

API_ID, API_HASH = credentials()
SINCE = datetime(2026, 6, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 1, tzinfo=timezone.utc)
OUT = Path('TGSpyder_Output/summer2026_sources')

CHANNELS = ['pressnbrb', 'skgovby', 'cyberpoolofsharks']
MY_POSTS = [('cyberpoleshuk', 1066), ('cyberpoleshuk', 1067), ('cyberpoleshuk', 1029)]


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    assert await client.is_user_authorized(), 'not authorized'
    OUT.mkdir(parents=True, exist_ok=True)

    mine = []
    for ch, mid in MY_POSTS:
        try:
            msg = await client.get_messages(ch, ids=mid)
            mine.append({'id': mid, 'date': str(msg.date), 'text': msg.text or ''})
        except Exception as e:
            mine.append({'id': mid, 'error': str(e)})
    (OUT / 'cyberpoleshuk_refs.json').write_text(
        json.dumps(mine, ensure_ascii=False, indent=2), encoding='utf-8')
    print('my posts:', [m.get('id') for m in mine])

    for ch in CHANNELS:
        posts = []
        try:
            entity = await client.get_entity(ch)
            async for msg in client.iter_messages(entity, offset_date=UNTIL, limit=None):
                if msg.date < SINCE:
                    break
                if not (msg.text or '').strip():
                    continue
                posts.append({
                    'id': msg.id,
                    'date': msg.date.isoformat(),
                    'url': f'https://t.me/{ch}/{msg.id}',
                    'views': msg.views or 0,
                    'text': msg.text,
                })
        except Exception as e:
            print(f'{ch}: ERROR {e}')
            continue
        posts.sort(key=lambda p: p['id'])
        (OUT / f'{ch}.json').write_text(
            json.dumps(posts, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'{ch}: {len(posts)} posts')

    await client.disconnect()


asyncio.run(main())
