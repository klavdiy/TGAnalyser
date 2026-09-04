"""Метрики рейтинга: охват ≈, читаемость, перцентиль сверстников, ERR₇."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

from rank_store import latest_snapshot_id

REACH_AGE_MIN_H = 24.0
REACH_AGE_MAX_H = 168.0
ERR7_TARGET_H = 168.0
ERR7_MIN_H = 144.0
ERR7_MAX_H = 192.0
EARLY_MIN_H = 12.0
EARLY_MAX_H = 36.0
ACTIVE_LOOKBACK_H = 14 * 24
ACTIVE_MIN_POSTS = 3
ERR7_MIN_POSTS = 10
PEER_RATIO_LO = 0.5
PEER_RATIO_HI = 2.0
PEER_MIN = 15
ABOUT_10K = 10_000
ABOUT_NEAR_10K = 8_000


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace('Z', '+00:00')
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def rkn_status(
    subscribers: int | None,
    verified: int | None,
    rkn_url: str | None,
    is_broadcast: int | None,
    error: str | None,
) -> str:
    if error or is_broadcast is None:
        return 'не проверено'
    if not is_broadcast:
        return 'не проверено'
    subs = subscribers or 0
    if subs >= ABOUT_10K:
        if verified:
            return 'A+'
        if rkn_url:
            return 'есть ссылка РКН'
        return '10К+ без подтверждения'
    if subs >= ABOUT_NEAR_10K:
        return 'около 10К'
    return 'не требуется'


def _snapshot_at(conn: sqlite3.Connection, snapshot_id: int) -> datetime:
    row = conn.execute(
        'SELECT collected_at FROM snapshots WHERE id = ?', (snapshot_id,)
    ).fetchone()
    if not row:
        raise SystemExit(f'Нет снимка #{snapshot_id}')
    ts = parse_ts(row['collected_at'])
    if ts is None:
        raise SystemExit(f'Битая дата снимка #{snapshot_id}')
    return ts


def catalog_rows(conn: sqlite3.Connection, snapshot_id: int | None = None) -> list[dict]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        raise SystemExit('В базе нет снимков — сначала collect_rank.py')

    snap_at = _snapshot_at(conn, snapshot_id)
    stats = conn.execute(
        """
        SELECT
            c.id, c.username, c.topic, c.panel,
            s.subscribers, s.last_post_at, s.posts_sampled,
            s.about, s.title, s.is_broadcast, s.is_megagroup,
            s.verified, s.rkn_url, s.error
        FROM channels c
        JOIN channel_stats s ON s.channel_id = c.id
        WHERE s.snapshot_id = ?
        ORDER BY c.id
        """,
        (snapshot_id,),
    ).fetchall()

    posts_by_channel: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT channel_id, msg_id, posted_at, views
        FROM post_views
        WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ):
        posts_by_channel.setdefault(row['channel_id'], []).append(row)

    prepared: list[dict] = []
    for row in stats:
        posts = posts_by_channel.get(row['id'], [])
        window_views: list[float] = []
        for post in posts:
            posted = parse_ts(post['posted_at'])
            if posted is None or post['views'] is None:
                continue
            age = hours_between(snap_at, posted)
            if REACH_AGE_MIN_H <= age <= REACH_AGE_MAX_H:
                window_views.append(float(post['views']))

        last_post = parse_ts(row['last_post_at'])
        hours_since_post = hours_between(snap_at, last_post) if last_post else None
        is_broadcast = row['is_broadcast']
        active = bool(
            is_broadcast
            and not row['error']
            and hours_since_post is not None
            and hours_since_post <= ACTIVE_LOOKBACK_H
            and len(window_views) >= ACTIVE_MIN_POSTS
        )
        reach = median(window_views) if window_views else None
        subscribers = row['subscribers']
        readability = None
        if active and reach is not None and subscribers:
            readability = reach / subscribers

        prepared.append({
            'snapshot_id': snapshot_id,
            'username': row['username'],
            'title': row['title'] or '',
            'about': row['about'] or '',
            'topic': row['topic'] or '',
            'panel': bool(row['panel']),
            'subscribers': subscribers,
            'reach': reach,
            'reach_approx': True,
            'readability': readability,
            'window_posts': len(window_views),
            'posts_sampled': row['posts_sampled'],
            'last_post_at': row['last_post_at'],
            'is_broadcast': is_broadcast,
            'is_megagroup': row['is_megagroup'],
            'verified': row['verified'],
            'rkn_url': row['rkn_url'] or '',
            'rkn_status': rkn_status(
                subscribers, row['verified'], row['rkn_url'], is_broadcast, row['error'],
            ),
            'error': row['error'],
            'active': active,
            'percentile': None,
            'place': None,
        })

    _assign_percentiles(prepared)
    _assign_places(prepared)
    return prepared


def _log_bucket(subscribers: int) -> int:
    return math.floor(math.log10(max(subscribers, 1)))


def _assign_percentiles(rows: list[dict]) -> None:
    active = [r for r in rows if r['active'] and r['readability'] is not None and r['subscribers']]
    for row in active:
        subs = row['subscribers']
        peers = [
            p for p in active
            if PEER_RATIO_LO * subs <= p['subscribers'] <= PEER_RATIO_HI * subs
        ]
        if len(peers) < PEER_MIN:
            bucket = _log_bucket(subs)
            peers = [p for p in active if _log_bucket(p['subscribers']) == bucket]
        if len(peers) <= 1:
            row['percentile'] = 100
            row['peer_count'] = len(peers)
            continue
        lower = sum(1 for p in peers if p['readability'] < row['readability'])
        row['percentile'] = int(round(100 * lower / (len(peers) - 1)))
        row['peer_count'] = len(peers)
    for row in rows:
        row.setdefault('peer_count', 0)


def _assign_places(rows: list[dict]) -> None:
    ranked = sorted(
        [r for r in rows if r['active'] and r['percentile'] is not None],
        key=lambda r: (-r['percentile'], -(r['readability'] or 0), -(r['subscribers'] or 0)),
    )
    for i, row in enumerate(ranked, 1):
        row['place'] = i


def _post_measurements(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            c.username, c.panel, c.id AS channel_id,
            pv.msg_id, pv.posted_at, pv.views,
            sn.collected_at, st.subscribers
        FROM post_views pv
        JOIN snapshots sn ON sn.id = pv.snapshot_id
        JOIN channels c ON c.id = pv.channel_id
        JOIN channel_stats st
            ON st.snapshot_id = pv.snapshot_id AND st.channel_id = pv.channel_id
        WHERE pv.views IS NOT NULL
          AND st.subscribers IS NOT NULL
          AND st.subscribers > 0
          AND st.error IS NULL
        ORDER BY c.id, pv.msg_id, sn.collected_at
        """
    ).fetchall()
    out = []
    for row in rows:
        posted = parse_ts(row['posted_at'])
        collected = parse_ts(row['collected_at'])
        if posted is None or collected is None:
            continue
        age = hours_between(collected, posted)
        if age < 0:
            continue
        out.append({
            'username': row['username'],
            'panel': bool(row['panel']),
            'msg_id': row['msg_id'],
            'age_h': age,
            'views': float(row['views']),
            'subscribers': float(row['subscribers']),
            'err': float(row['views']) / float(row['subscribers']),
        })
    return out


def _pick_err7(measurements: list[dict]) -> dict | None:
    eligible = [m for m in measurements if ERR7_MIN_H <= m['age_h'] <= ERR7_MAX_H]
    if not eligible:
        return None
    return min(eligible, key=lambda m: abs(m['age_h'] - ERR7_TARGET_H))


def err7_rows(
    conn: sqlite3.Connection,
    panel_only: bool = True,
    min_posts: int = ERR7_MIN_POSTS,
) -> list[dict]:
    by_post: dict[tuple[str, int], list[dict]] = {}
    for item in _post_measurements(conn):
        if panel_only and not item['panel']:
            continue
        by_post.setdefault((item['username'], item['msg_id']), []).append(item)

    by_channel: dict[str, list[float]] = {}
    post_count: dict[str, int] = {}
    for key, series in by_post.items():
        picked = _pick_err7(series)
        if picked is None:
            continue
        username = key[0]
        by_channel.setdefault(username, []).append(picked['err'])
        post_count[username] = post_count.get(username, 0) + 1

    rows = []
    for username, errs in by_channel.items():
        if len(errs) < min_posts:
            continue
        rows.append({
            'username': username,
            'err7': median(errs),
            'posts': len(errs),
        })
    rows.sort(key=lambda r: (-(r['err7'] or 0), -r['posts'], r['username']))
    _assign_err7_places(rows)
    return rows


def _assign_err7_places(rows: list[dict]) -> None:
    """Близкие медианы (Δ < 0.5 п.п.) делят место, как в статье."""
    place = 1
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows) and abs((rows[j]['err7'] or 0) - (rows[i]['err7'] or 0)) < 0.005:
            j += 1
        if j - i > 1:
            label = f'{place}–{place + (j - i) - 1}'
            note = 'разница статистически незначима, порядок редакционный'
        else:
            label = str(place)
            note = ''
        for row in rows[i:j]:
            row['place'] = label
            row['note'] = note
        place += j - i
        i = j


def paired_err_growth(conn: sqlite3.Connection) -> dict:
    """Медианный рост ERR между ранним (~24ч) и недельным (~168ч) замерами."""
    by_post: dict[tuple[str, int], list[dict]] = {}
    for item in _post_measurements(conn):
        by_post.setdefault((item['username'], item['msg_id']), []).append(item)

    ratios = []
    deltas = []
    for series in by_post.values():
        early = [m for m in series if EARLY_MIN_H <= m['age_h'] <= EARLY_MAX_H]
        weekly = [m for m in series if ERR7_MIN_H <= m['age_h'] <= ERR7_MAX_H]
        if not early or not weekly:
            continue
        e = min(early, key=lambda m: abs(m['age_h'] - 24.0))
        w = min(weekly, key=lambda m: abs(m['age_h'] - ERR7_TARGET_H))
        if e['err'] <= 0:
            continue
        ratios.append(w['err'] / e['err'])
        deltas.append(w['err'] - e['err'])
    return {
        'pairs': len(ratios),
        'median_ratio': median(ratios),
        'median_delta': median(deltas),
    }
