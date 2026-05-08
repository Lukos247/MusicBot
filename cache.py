from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_cache (
    video_id  TEXT PRIMARY KEY,
    file_id   TEXT NOT NULL,
    title     TEXT,
    performer TEXT,
    duration  INTEGER,
    cached_at INTEGER DEFAULT (strftime('%s', 'now'))
);
"""


@dataclass(slots=True)
class CachedTrack:
    video_id: str
    file_id: str
    title: str
    performer: str
    duration: int


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def get(video_id: str) -> CachedTrack | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT video_id, file_id, title, performer, duration "
            "FROM track_cache WHERE video_id = ?",
            (video_id,),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return CachedTrack(
                video_id=row[0],
                file_id=row[1],
                title=row[2] or "",
                performer=row[3] or "",
                duration=row[4] or 0,
            )


async def save(
    video_id: str,
    file_id: str,
    title: str,
    performer: str,
    duration: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO track_cache (video_id, file_id, title, performer, duration) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET "
            "file_id = excluded.file_id, "
            "title = excluded.title, "
            "performer = excluded.performer, "
            "duration = excluded.duration, "
            "cached_at = strftime('%s', 'now')",
            (video_id, file_id, title, performer, duration),
        )
        await db.commit()


async def get_many(video_ids: list[str]) -> dict[str, CachedTrack]:
    if not video_ids:
        return {}
    placeholders = ",".join("?" * len(video_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT video_id, file_id, title, performer, duration "
            f"FROM track_cache WHERE video_id IN ({placeholders})",
            video_ids,
        ) as cur:
            rows = await cur.fetchall()
    return {
        row[0]: CachedTrack(
            video_id=row[0],
            file_id=row[1],
            title=row[2] or "",
            performer=row[3] or "",
            duration=row[4] or 0,
        )
        for row in rows
    }


async def search_text(query: str, limit: int) -> list[CachedTrack]:
    """Fuzzy text search across cached tracks. Tokenizes the query on
    whitespace and requires every token to match (case-insensitive,
    substring) either the title or the performer. Recent tracks first."""
    tokens = [t for t in query.lower().split() if t]
    if not tokens:
        return []

    where_parts = []
    params: list = []
    for token in tokens:
        where_parts.append("(LOWER(title) LIKE ? OR LOWER(performer) LIKE ?)")
        like = f"%{token}%"
        params.extend([like, like])
    sql = (
        "SELECT video_id, file_id, title, performer, duration "
        "FROM track_cache "
        f"WHERE {' AND '.join(where_parts)} "
        "ORDER BY cached_at DESC "
        "LIMIT ?"
    )
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [
        CachedTrack(
            video_id=row[0],
            file_id=row[1],
            title=row[2] or "",
            performer=row[3] or "",
            duration=row[4] or 0,
        )
        for row in rows
    ]
