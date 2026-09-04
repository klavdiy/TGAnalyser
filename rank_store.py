"""SQLite-хранилище снимков рейтинга и загрузка каталога."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / 'data' / 'catalog.yaml'
DEFAULT_DB = ROOT / 'data' / 'rank.sqlite'

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    tg_id         INTEGER,
    title         TEXT,
    about         TEXT,
    topic         TEXT,
    panel         INTEGER NOT NULL DEFAULT 0,
    is_broadcast  INTEGER,
    is_megagroup  INTEGER,
    verified      INTEGER,
    rkn_url       TEXT,
    last_ok       TEXT,
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    collected_at  TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS channel_stats (
    snapshot_id    INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    subscribers    INTEGER,
    last_post_at   TEXT,
    posts_sampled  INTEGER NOT NULL DEFAULT 0,
    about          TEXT,
    title          TEXT,
    is_broadcast   INTEGER,
    is_megagroup   INTEGER,
    verified       INTEGER,
    rkn_url        TEXT,
    error          TEXT,
    PRIMARY KEY (snapshot_id, channel_id),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id),
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);

CREATE TABLE IF NOT EXISTS post_views (
    snapshot_id  INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    msg_id       INTEGER NOT NULL,
    posted_at    TEXT NOT NULL,
    views        INTEGER,
    forwards     INTEGER,
    reactions    INTEGER,
    react_pos    INTEGER,
    react_neu    INTEGER,
    react_neg    INTEGER,
    body         TEXT,
    links        TEXT,
    fwd_from     TEXT,
    PRIMARY KEY (snapshot_id, channel_id, msg_id),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id),
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);

CREATE INDEX IF NOT EXISTS idx_post_views_channel
    ON post_views (channel_id, msg_id, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_channel_stats_snap
    ON channel_stats (snapshot_id);
"""


def load_catalog(path: Path = DEFAULT_CATALOG) -> list[dict]:
    """Читает ограниченный YAML каталога без внешних зависимостей."""
    if not path.exists():
        raise SystemExit(f'Нет каталога: {path}')

    channels: list[dict] = []
    current: dict | None = None
    for line in path.read_text(encoding='utf-8').splitlines():
        raw = line.strip()
        if not raw or raw.startswith('#'):
            continue
        if raw.startswith('- username:'):
            if current:
                channels.append(current)
            username = raw.split(':', 1)[1].strip().lstrip('@')
            current = {'username': username, 'topic': '', 'panel': False, 'me': False}
        elif current is not None and raw.startswith('topic:'):
            current['topic'] = raw.split(':', 1)[1].strip()
        elif current is not None and raw.startswith('panel:'):
            current['panel'] = raw.split(':', 1)[1].strip().lower() in (
                'true', 'yes', '1',
            )
        elif current is not None and raw.startswith('me:'):
            current['me'] = raw.split(':', 1)[1].strip().lower() in (
                'true', 'yes', '1',
            )
    if current:
        channels.append(current)
    if not channels:
        raise SystemExit(f'Каталог пуст: {path}')
    return channels


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute('PRAGMA table_info(post_views)')}
    for name in ('react_pos', 'react_neu', 'react_neg'):
        if name not in cols:
            conn.execute(f'ALTER TABLE post_views ADD COLUMN {name} INTEGER')
    for name in ('body', 'links', 'fwd_from'):
        if name not in cols:
            conn.execute(f'ALTER TABLE post_views ADD COLUMN {name} TEXT')
    conn.commit()


def _links_cell(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def upsert_catalog(conn: sqlite3.Connection, catalog: list[dict]) -> dict[str, int]:
    """Синхронизирует username/topic/panel из YAML. Возвращает username → id."""
    ids: dict[str, int] = {}
    for item in catalog:
        username = item['username']
        conn.execute(
            """
            INSERT INTO channels (username, topic, panel)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                topic = excluded.topic,
                panel = excluded.panel
            """,
            (username, item.get('topic') or '', 1 if item.get('panel') else 0),
        )
        row = conn.execute(
            'SELECT id FROM channels WHERE username = ? COLLATE NOCASE',
            (username,),
        ).fetchone()
        ids[username.lower()] = row['id']
    conn.commit()
    return ids


def create_snapshot(conn: sqlite3.Connection, notes: str = '') -> int:
    cur = conn.execute(
        'INSERT INTO snapshots (collected_at, notes) VALUES (?, ?)',
        (utc_now(), notes),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_snapshot_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute('SELECT id FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    return int(row['id']) if row else None


def snapshot_done_usernames(conn: sqlite3.Connection, snapshot_id: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT c.username
        FROM channel_stats s
        JOIN channels c ON c.id = s.channel_id
        WHERE s.snapshot_id = ? AND s.error IS NULL
        """,
        (snapshot_id,),
    ).fetchall()
    return {r['username'].lower() for r in rows}


def snapshot_usernames(conn: sqlite3.Connection, snapshot_id: int | None = None) -> list[str]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        return []
    rows = conn.execute(
        """
        SELECT c.username
        FROM channel_stats s
        JOIN channels c ON c.id = s.channel_id
        WHERE s.snapshot_id = ?
        ORDER BY c.username COLLATE NOCASE
        """,
        (snapshot_id,),
    ).fetchall()
    return [r['username'] for r in rows]


def replace_channel_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: int,
    channel_id: int,
    stats: dict,
    posts: list[dict],
) -> None:
    conn.execute(
        'DELETE FROM post_views WHERE snapshot_id = ? AND channel_id = ?',
        (snapshot_id, channel_id),
    )
    conn.execute(
        'DELETE FROM channel_stats WHERE snapshot_id = ? AND channel_id = ?',
        (snapshot_id, channel_id),
    )
    conn.execute(
        """
        INSERT INTO channel_stats (
            snapshot_id, channel_id, subscribers, last_post_at, posts_sampled,
            about, title, is_broadcast, is_megagroup, verified, rkn_url, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            channel_id,
            stats.get('subscribers'),
            stats.get('last_post_at'),
            stats.get('posts_sampled', 0),
            stats.get('about'),
            stats.get('title'),
            stats.get('is_broadcast'),
            stats.get('is_megagroup'),
            stats.get('verified'),
            stats.get('rkn_url'),
            stats.get('error'),
        ),
    )
    conn.executemany(
        """
        INSERT INTO post_views (
            snapshot_id, channel_id, msg_id, posted_at, views, forwards,
            reactions, react_pos, react_neu, react_neg, body, links, fwd_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_id,
                channel_id,
                p['msg_id'],
                p['posted_at'],
                p.get('views'),
                p.get('forwards'),
                p.get('reactions'),
                p.get('react_pos'),
                p.get('react_neu'),
                p.get('react_neg'),
                p.get('body'),
                _links_cell(p.get('links')),
                p.get('fwd_from'),
            )
            for p in posts
        ],
    )
    error = stats.get('error')
    conn.execute(
        """
        UPDATE channels SET
            tg_id = COALESCE(?, tg_id),
            title = COALESCE(?, title),
            about = COALESCE(?, about),
            is_broadcast = COALESCE(?, is_broadcast),
            is_megagroup = COALESCE(?, is_megagroup),
            verified = COALESCE(?, verified),
            rkn_url = COALESCE(?, rkn_url),
            last_ok = CASE WHEN ? IS NULL THEN ? ELSE last_ok END,
            last_error = ?
        WHERE id = ?
        """,
        (
            stats.get('tg_id'),
            stats.get('title'),
            stats.get('about'),
            stats.get('is_broadcast'),
            stats.get('is_megagroup'),
            stats.get('verified'),
            stats.get('rkn_url'),
            error,
            utc_now(),
            error,
            channel_id,
        ),
    )
    conn.commit()
