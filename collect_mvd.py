import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient

from tg_config import SESSION, credentials

API_ID, API_HASH = credentials()
CHANNEL = 'pressmvd'
TAG = 'мвд_стоп_мошенники'
SINCE = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
OUT = Path('TGSpyder_Output/pressmvd/mvd_stop_jun_aug_2026')

QR_TIMEOUT = 900

QR_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Telegram QR</title>
<style>
 body{font-family:-apple-system,sans-serif;text-align:center;padding:24px;background:#f5f5f7}
 img{width:340px;height:340px;image-rendering:pixelated;background:#fff;padding:12px;border-radius:12px;
     box-shadow:0 2px 12px rgba(0,0,0,.12)}
 p{color:#555}
</style>
<h2>Вход в Telegram</h2>
<p>Telegram → Настройки → Устройства → Подключить устройство → сканируйте код</p>
<img id="q" src="qr_login.png">
<p id="s">код обновляется автоматически</p>
<script>
setInterval(()=>{document.getElementById('q').src='qr_login.png?t='+Date.now()},3000);
</script>
"""


def has_tag(text: str) -> bool:
    if not text:
        return False
    return any(
        m.group(1).lower() == TAG
        for m in re.finditer(r'#([\w\u0400-\u04FF]+)', text, re.UNICODE)
    )


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        import qrcode
        from telethon.errors import SessionPasswordNeededError

        Path('qr.html').write_text(QR_PAGE, encoding='utf-8')
        qr = await client.qr_login()
        deadline = asyncio.get_event_loop().time() + QR_TIMEOUT
        while True:
            Path('qr_url.txt').write_text(qr.url)
            img = qrcode.QRCode(box_size=12, border=3)
            img.add_data(qr.url)
            img.make(fit=True)
            img.make_image().save('qr_login.png')
            print(f'QR refreshed {datetime.now():%H:%M:%S} url={qr.url}')
            sys.stdout.flush()
            try:
                await qr.wait(timeout=25)
                break
            except asyncio.TimeoutError:
                if asyncio.get_event_loop().time() > deadline:
                    print('QR TIMEOUT')
                    return
                await qr.recreate()
            except SessionPasswordNeededError:
                pwd_file = Path('password.txt')
                if not pwd_file.exists():
                    print('NEED_2FA_PASSWORD: создайте файл password.txt с облачным паролем')
                    return
                await client.sign_in(password=pwd_file.read_text().strip())
                break

    me = await client.get_me()
    print(f'AUTH OK: {me.first_name} @{me.username} {me.phone}')
    sys.stdout.flush()

    if '--check-only' in sys.argv:
        await client.disconnect()
        return

    OUT.mkdir(parents=True, exist_ok=True)
    entity = await client.get_entity(CHANNEL)
    posts = []
    scanned = 0
    async for msg in client.iter_messages(entity, offset_date=UNTIL, limit=None):
        if msg.date < SINCE:
            break
        scanned += 1
        if not has_tag(msg.text or ''):
            continue
        posts.append({
            'id': msg.id,
            'date': msg.date.isoformat(),
            'url': f'https://t.me/{CHANNEL}/{msg.id}',
            'views': msg.views or 0,
            'forwards': msg.forwards or 0,
            'text': msg.text or '',
        })
        if len(posts) % 20 == 0:
            print(f'scanned={scanned} tagged={len(posts)}')
            sys.stdout.flush()

    await client.disconnect()
    posts.sort(key=lambda p: p['id'])
    (OUT / 'posts.json').write_text(
        json.dumps(posts, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(f'DONE scanned={scanned} tagged={len(posts)} -> {OUT / "posts.json"}')


asyncio.run(main())
