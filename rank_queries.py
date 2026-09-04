"""Загрузка постов канала из снимка рейтинга."""

from __future__ import annotations

import sqlite3

from rank_store import latest_snapshot_id, snapshot_usernames as _snapshot_usernames


def snapshot_meta(conn: sqlite3.Connection, snapshot_id: int | None = None) -> dict:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        return {}
    row = conn.execute(
        'SELECT id, collected_at, notes FROM snapshots WHERE id = ?',
        (snapshot_id,),
    ).fetchone()
    if not row:
        return {}
    n = conn.execute(
        'SELECT COUNT(*) AS n FROM channel_stats WHERE snapshot_id = ?',
        (snapshot_id,),
    ).fetchone()['n']
    return {
        'id': row['id'],
        'collected_at': row['collected_at'],
        'notes': row['notes'] or '',
        'channels': n,
    }


def snapshot_usernames(conn: sqlite3.Connection, snapshot_id: int | None = None) -> list[str]:
    return _snapshot_usernames(conn, snapshot_id)


def channel_posts(
    conn: sqlite3.Connection,
    username: str,
    snapshot_id: int | None = None,
) -> list[dict]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        return []
    rows = conn.execute(
        """
        SELECT pv.msg_id, pv.posted_at, pv.views, pv.forwards, pv.reactions,
               pv.react_pos, pv.react_neu, pv.react_neg,
               pv.body, pv.links, pv.fwd_from,
               c.username
        FROM post_views pv
        JOIN channels c ON c.id = pv.channel_id
        WHERE pv.snapshot_id = ? AND c.username = ? COLLATE NOCASE
        ORDER BY pv.posted_at
        """,
        (snapshot_id, username.lstrip('@')),
    ).fetchall()
    return [dict(r) for r in rows]


def subscriber_history(conn: sqlite3.Connection, username: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.collected_at, st.subscribers, st.posts_sampled
        FROM channel_stats st
        JOIN channels c ON c.id = st.channel_id
        JOIN snapshots s ON s.id = st.snapshot_id
        WHERE c.username = ? COLLATE NOCASE
        ORDER BY s.collected_at
        """,
        (username.lstrip('@'),),
    ).fetchall()
    return [dict(r) for r in rows]


def previous_snapshot_id(conn: sqlite3.Connection, snapshot_id: int) -> int | None:
    row = conn.execute(
        'SELECT id FROM snapshots WHERE id < ? ORDER BY id DESC LIMIT 1',
        (snapshot_id,),
    ).fetchone()
    return int(row['id']) if row else None


def channel_post_archive(conn: sqlite3.Connection, username: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pv.msg_id AS id, MIN(pv.posted_at) AS posted_at
        FROM post_views pv
        JOIN channels c ON c.id = pv.channel_id
        WHERE c.username = ? COLLATE NOCASE
        GROUP BY pv.msg_id
        ORDER BY posted_at
        """,
        (username.lstrip('@'),),
    ).fetchall()
    return [dict(r) for r in rows]
