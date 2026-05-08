from __future__ import annotations

import asyncio

_inflight: dict[str, asyncio.Event] = {}


async def acquire(video_id: str) -> asyncio.Event | None:
    """Single-flight gate per video_id.

    If a download for this video_id is already in progress, await its
    completion and return None — the caller should then re-read the cache.
    Otherwise reserve the slot and return an Event the caller MUST set()
    via release() in a finally block.
    """
    existing = _inflight.get(video_id)
    if existing is not None:
        await existing.wait()
        return None
    ev = asyncio.Event()
    _inflight[video_id] = ev
    return ev


def release(video_id: str, ev: asyncio.Event) -> None:
    ev.set()
    _inflight.pop(video_id, None)
