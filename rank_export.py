"""Экспорт рейтинга каталога и ERR₇ в Excel и Markdown."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from rank_metrics import (
    ACTIVE_LOOKBACK_H,
    ACTIVE_MIN_POSTS,
    ERR7_MAX_H,
    ERR7_MIN_H,
    ERR7_MIN_POSTS,
    ERR7_TARGET_H,
    PEER_MIN,
    PEER_RATIO_HI,
    PEER_RATIO_LO,
    REACH_AGE_MAX_H,
    REACH_AGE_MIN_H,
    catalog_rows,
    err7_rows,
    paired_err_growth,
    parse_ts,
)
from rank_queries import channel_post_archive, channel_posts, previous_snapshot_id, snapshot_meta, subscriber_history
from rank_store import DEFAULT_DB, connect, latest_snapshot_id, load_catalog

OUT_DIR = Path('TGSpyder_Output/rank')


def _fmt_int(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '—'
    return f'{int(round(value)):,}'.replace(',', ' ')


def _fmt_pct(value, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '—'
    return f'{value * 100:.{digits}f}%'.replace('.', ',')


def _fmt_date(value: str | None) -> str:
    if not value:
        return '—'
    return value[:10]


def _catalog_sort_key(row: dict) -> tuple:
    place = row.get('place')
    if place is None:
        return (1, 10**9, row['username'])
    return (0, int(place), row['username'])


def catalog_dataframe(rows: list[dict]) -> pd.DataFrame:
    records = []
    for row in sorted(rows, key=_catalog_sort_key):
        reach_label = '—'
        if row['reach'] is not None:
            mark = '≈1–7 дн.' if row['reach_approx'] else ''
            reach_label = f'{_fmt_int(row["reach"])}{mark}'
        percentile_label = '—'
        if row['percentile'] is not None and row['active']:
            percentile_label = f'P{row["percentile"]}'
        records.append({
            'Место': str(row['place']) if row['place'] is not None else '—',
            'Канал': f'@{row["username"]}',
            'Название': row['title'],
            'Описание': (row['about'] or '')[:500],
            'Тематика': row['topic'],
            'Подписчики': row['subscribers'],
            'Охват': None if row['reach'] is None else round(row['reach']),
            'Охват_подпись': reach_label,
            'Читаемость': None if row['readability'] is None else round(row['readability'] * 100, 1),
            'Читаемость_подпись': _fmt_pct(row['readability']),
            'Перцентиль': row['percentile'],
            'Перцентиль_подпись': percentile_label,
            'Постов_в_окне': row['window_posts'],
            'Последний_пост': _fmt_date(row['last_post_at']),
            'A+_РКН': row['rkn_status'],
            'РКН_ссылка': row['rkn_url'],
            'Активный': 'да' if row['active'] else 'нет',
            'Тип': 'канал' if row['is_broadcast'] else ('чат' if row['is_megagroup'] else '—'),
            'Панель_ERR7': 'да' if row['panel'] else 'нет',
            'Ошибка': row['error'] or '',
        })
    return pd.DataFrame(records)


def err7_dataframe(rows: list[dict]) -> pd.DataFrame:
    records = []
    for row in rows:
        records.append({
            'Место': row['place'],
            'Канал': f'@{row["username"]}',
            'Медианный_ERR7': None if row['err7'] is None else round(row['err7'] * 100, 1),
            'ERR7_подпись': _fmt_pct(row['err7']),
            'Публикаций': row['posts'],
            'Комментарий': row.get('note') or '',
        })
    return pd.DataFrame(records)


def methodology_dataframe(snapshot_id: int, n_catalog: int, n_active: int, growth: dict) -> pd.DataFrame:
    ratio = growth.get('median_ratio')
    delta = growth.get('median_delta')
    ratio_s = '—' if ratio is None else f'{ratio:.2f}×'
    delta_s = '—' if delta is None else f'{delta * 100:.1f} п.п.'.replace('.', ',')
    rows = [
        ('Снимок', str(snapshot_id)),
        ('Окно постов, дни', '14'),
        ('Охват ≈', f'медиана просмотров постов возрастом {int(REACH_AGE_MIN_H)}–{int(REACH_AGE_MAX_H)} ч'),
        ('Активный канал', (
            f'broadcast, пост за {ACTIVE_LOOKBACK_H // 24} дн., '
            f'≥{ACTIVE_MIN_POSTS} постов в окне охвата'
        )),
        ('Читаемость', 'охват / подписчики'),
        ('Сверстники', (
            f'подписчики в [{PEER_RATIO_LO}×, {PEER_RATIO_HI}×]; '
            f'если их <{PEER_MIN} — логарифмическая корзина размера'
        )),
        ('Перцентиль', 'доля сверстников с меньшей читаемостью, P0–P100'),
        ('ERR₇', (
            f'замер просмотров с возрастом {int(ERR7_MIN_H)}–{int(ERR7_MAX_H)} ч, '
            f'ближайший к {int(ERR7_TARGET_H)} ч; ERR = views / subscribers; медиана по каналу'
        )),
        ('Порог ERR₇', f'≥{ERR7_MIN_POSTS} зрелых публикаций, только panel: true'),
        ('Каналов в снимке', str(n_catalog)),
        ('Активных в рейтинге', str(n_active)),
        ('Парных замеров ERR (ранний+недельный)', str(growth.get('pairs') or 0)),
        ('Медианный рост ERR (отношение)', ratio_s),
        ('Медианный рост ERR (п.п.)', delta_s),
        ('Источник', 'Telegram API'),
    ]
    return pd.DataFrame(rows, columns=['Параметр', 'Значение'])


def write_markdown_catalog(df: pd.DataFrame, path: Path) -> None:
    lines = [
        '# Рейтинг каталога по читаемости',
        '',
        'Место только у активных каналов. Охват — медиана просмотров постов возрастом 24 ч–7 дн. (≈).',
        '',
        '| Место | Канал | Тематика | Подписчики | Охват | Читаемость | Перцентиль | Последний пост | A+ / РКН |',
        '| --- | --- | --- | ---: | --- | --- | --- | --- | --- |',
    ]
    for rec in df.to_dict(orient='records'):
        extra = ''
        if rec['Перцентиль_подпись'] != '—':
            extra = f" {rec['Постов_в_окне']} пост."
        lines.append(
            '| {place} | {ch} | {topic} | {subs} | {reach} | {read} | {pct} | {last} | {rkn} |'.format(
                place=rec['Место'],
                ch=rec['Канал'],
                topic=rec['Тематика'],
                subs=_fmt_int(rec['Подписчики']),
                reach=rec['Охват_подпись'],
                read=rec['Читаемость_подпись'],
                pct=rec['Перцентиль_подпись'] + extra,
                last=rec['Последний_пост'],
                rkn=rec['A+_РКН'],
            )
        )
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_markdown_err7(df: pd.DataFrame, path: Path) -> None:
    lines = [
        '# Рейтинг ERR₇',
        '',
        'Медиана доли подписчиков, досмотревших публикацию примерно через 7 суток.',
        '',
        '| Место | Канал | Медианный ERR₇ | Публикаций |',
        '| --- | --- | ---: | ---: |',
    ]
    if df.empty:
        lines.append('| — | недостаточно зрелых постов | — | — |')
    else:
        for rec in df.to_dict(orient='records'):
            lines.append(
                f"| {rec['Место']} | {rec['Канал']} | {rec['ERR7_подпись']} | {rec['Публикаций']} |"
            )
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def export(db: Path, out_dir: Path, snapshot_id: int | None) -> Path:
    conn = connect(db)
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        raise SystemExit('В базе нет снимков — сначала python collect_rank.py')

    cat_rows = catalog_rows(conn, snapshot_id)
    err_rows = err7_rows(conn, panel_only=True)
    watch = {c['username'].lower() for c in load_catalog()}
    cat_rows = [r for r in cat_rows if r['username'].lower() in watch]
    err_rows = [r for r in err_rows if r['username'].lower() in watch]
    growth = paired_err_growth(conn)
    conn.close()

    df_cat = catalog_dataframe(cat_rows)
    df_err = err7_dataframe(err_rows)
    n_active = sum(1 for r in cat_rows if r['active'])
    df_meta = methodology_dataframe(snapshot_id, len(cat_rows), n_active, growth)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    xlsx = out_dir / f'rank_{stamp}.xlsx'
    md_cat = out_dir / f'rank_catalog_{stamp}.md'
    md_err = out_dir / f'rank_err7_{stamp}.md'

    with pd.ExcelWriter(xlsx, engine='openpyxl') as writer:
        df_cat.to_excel(writer, sheet_name='Catalog', index=False)
        df_err.to_excel(writer, sheet_name='ERR7', index=False)
        df_meta.to_excel(writer, sheet_name='Methodology', index=False)
        for sheet in writer.sheets.values():
            for col in sheet.columns:
                width = max((len(str(c.value)) for c in col if c.value), default=10)
                sheet.column_dimensions[col[0].column_letter].width = min(width + 2, 70)

    write_markdown_catalog(df_cat, md_cat)
    write_markdown_err7(df_err, md_err)
    print(f'Excel     → {xlsx}')
    print(f'Каталог   → {md_cat}  ({n_active} активных / {len(cat_rows)})')
    print(f'ERR7      → {md_err}  ({len(err_rows)} каналов)')
    return xlsx


_LOCAL_TZ = ZoneInfo('Europe/Minsk')


def _growth(hist: list[dict]) -> dict:
    points = [
        (h['collected_at'], h['subscribers'])
        for h in hist
        if h.get('subscribers') is not None
    ]
    if not points:
        return {'delta': None, 'delta_pct': None, 'delta_all': None}
    last = points[-1][1]
    prev = points[-2][1] if len(points) > 1 else None
    first = points[0][1]
    delta = None if prev is None else last - prev
    delta_pct = None if prev in (None, 0) else round(100 * delta / prev, 1)
    delta_all = last - first if len(points) > 1 else None
    return {'delta': delta, 'delta_pct': delta_pct, 'delta_all': delta_all}


def _cadence(times: list[datetime], now: datetime) -> dict:
    def since(days: int) -> int:
        cut = now - timedelta(days=days)
        return sum(1 for t in times if t >= cut)

    span = None
    if times:
        span = round((now - min(times)).total_seconds() / 86400, 1)
    return {'d7': since(7), 'd30': since(30), 'd90': since(90), 'span_days': span}


def _heatmap(times: list[datetime]) -> list[list[int]]:
    grid = [[0] * 24 for _ in range(7)]
    for t in times:
        local = t.astimezone(_LOCAL_TZ)
        grid[local.weekday()][local.hour] += 1
    return grid


def _metric_delta(current, previous, key: str):
    if previous is None:
        return None
    cur = current.get(key)
    prev = previous.get(key)
    if cur is None or prev is None:
        return None
    if key == 'readability':
        return round((cur - prev) * 100, 1)
    return round(cur - prev)


_FORBIDDEN_TOKENS = (
    'api_hash', 'api_id', 'tg_api', 'session', 'password', 'passwd',
    'rkn', 'gosuslugi', 'knd.gov',
)


def _assert_public(payload: dict) -> None:
    blob = json.dumps(payload, ensure_ascii=False).lower()
    for token in _FORBIDDEN_TOKENS:
        if token in blob:
            raise SystemExit('public export contains a forbidden field')


def _parse_links(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(u) for u in raw if u]
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(u) for u in value if u]


def _post_public(p: dict) -> dict:
    return {
        'id': p['msg_id'],
        'posted_at': p['posted_at'],
        'views': p['views'],
        'forwards': p['forwards'],
        'reactions': p.get('reactions'),
        'react_pos': p.get('react_pos'),
        'react_neu': p.get('react_neu'),
        'react_neg': p.get('react_neg'),
    }


def _post_admin(p: dict) -> dict:
    item = _post_public(p)
    item['text'] = p.get('body') or ''
    item['links'] = _parse_links(p.get('links'))
    item['fwd'] = p.get('fwd_from') or None
    return item


def _snapshot_payload(conn, snapshot_id: int, *, private: bool) -> dict:
    meta = snapshot_meta(conn, snapshot_id)
    rows = catalog_rows(conn, snapshot_id)
    catalog = load_catalog()
    watch = {c['username'].lower() for c in catalog}
    me_set = {c['username'].lower() for c in catalog if c.get('me')}
    rows = [r for r in rows if r['username'].lower() in watch]
    prev_id = previous_snapshot_id(conn, snapshot_id)
    prev_map = {}
    if prev_id is not None:
        prev_map = {
            r['username'].lower(): r
            for r in catalog_rows(conn, prev_id)
            if r['username'].lower() in watch
        }
    now = parse_ts(meta.get('collected_at')) or datetime.now(timezone.utc)
    combined_times: list[datetime] = []
    channels = []
    post_fn = _post_admin if private else _post_public
    for row in sorted(rows, key=_catalog_sort_key):
        posts = channel_posts(conn, row['username'], snapshot_id)
        hist = subscriber_history(conn, row['username'])
        archive = channel_post_archive(conn, row['username'])
        times = [t for t in (parse_ts(p['posted_at']) for p in archive) if t]
        combined_times.extend(times)
        prev = prev_map.get(row['username'].lower())
        channel = {
            'place': row['place'],
            'username': row['username'],
            'title': row['title'] or '',
            'topic': row['topic'] or '',
            'subscribers': row['subscribers'],
            'reach': None if row['reach'] is None else round(row['reach']),
            'readability': None if row['readability'] is None else round(row['readability'] * 100, 1),
            'percentile': row['percentile'],
            'posts_1_7d': row['window_posts'],
            'last_post': (row['last_post_at'] or '')[:10] or None,
            'active': bool(row['active']),
            'kind': 'channel' if row['is_broadcast'] else ('chat' if row['is_megagroup'] else None),
            'growth': _growth(hist),
            'cadence': _cadence(times, now),
            'heatmap': _heatmap(times),
            'delta_reach': _metric_delta(row, prev, 'reach'),
            'delta_readability': _metric_delta(row, prev, 'readability'),
            'posts': [post_fn(p) for p in posts],
            'subscriber_history': [
                {'at': h['collected_at'], 'subscribers': h['subscribers']}
                for h in hist
            ],
        }
        if private:
            channel['about'] = row.get('about') or ''
            channel['me'] = row['username'].lower() in me_set
        channels.append(channel)
    payload = {
        'collected_at': meta.get('collected_at'),
        'snapshot_id': meta.get('id'),
        'heatmap': _heatmap(combined_times),
        'channels': channels,
    }
    if private:
        payload['catalog'] = [
            {
                'username': c['username'],
                'topic': c.get('topic') or '',
                'panel': bool(c.get('panel')),
                'me': bool(c.get('me')),
            }
            for c in catalog
        ]
    return payload


def public_payload(conn, snapshot_id: int) -> dict:
    payload = _snapshot_payload(conn, snapshot_id, private=False)
    _assert_public(payload)
    return payload


def admin_payload(conn, snapshot_id: int) -> dict:
    return _snapshot_payload(conn, snapshot_id, private=True)


def _write_json(path: Path, payload: dict, label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{label} → {path}')
    return path


def write_public_json(db: Path, path: Path, snapshot_id: int | None) -> Path:
    conn = connect(db)
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        conn.close()
        raise SystemExit('В базе нет снимков — сначала python collect_rank.py')
    payload = public_payload(conn, snapshot_id)
    conn.close()
    return _write_json(path, payload, 'JSON     ')


def write_admin_json(db: Path, path: Path, snapshot_id: int | None) -> Path:
    conn = connect(db)
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        conn.close()
        raise SystemExit('В базе нет снимков — сначала python collect_rank.py')
    payload = admin_payload(conn, snapshot_id)
    conn.close()
    return _write_json(path, payload, 'Admin JSON')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Экспорт рейтинга каналов')
    parser.add_argument('--db', default=str(DEFAULT_DB))
    parser.add_argument('--out', default=str(OUT_DIR))
    parser.add_argument('--snapshot', type=int, default=0, help='id снимка, 0 = последний')
    parser.add_argument('--json', default='', help='путь к публичному JSON (без Excel)')
    parser.add_argument(
        '--json-admin', default='',
        help='закрытый JSON (тексты, ссылки, about) — не класть в публичный репозиторий',
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    snapshot_id = args.snapshot or None
    wrote = False
    if args.json:
        write_public_json(Path(args.db), Path(args.json), snapshot_id)
        wrote = True
    if args.json_admin:
        write_admin_json(Path(args.db), Path(args.json_admin), snapshot_id)
        wrote = True
    if wrote:
        return
    export(Path(args.db), Path(args.out), snapshot_id)


if __name__ == '__main__':
    main()
