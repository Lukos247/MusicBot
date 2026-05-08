"""Source router: VK Music first, YouTube/yt-dlp as fallback.

Handlers import from this module instead of `youtube` directly. The
choice between sources is implicit:

- search() asks VK if VK_TOKEN is configured. If VK has results, return
  them. Otherwise fall through to YouTube unless DISABLE_YOUTUBE_FALLBACK
  is set.
- download() routes by video_id format. VK ids look like "-?\\d+_\\d+"
  (e.g. "12345_67890" or "-200_67890"); YouTube ids are 11-char
  alphanumeric/dash/underscore strings (e.g. "dQw4w9WgXcQ").

TrackMeta/DownloadedTrack/FileTooLargeError/cleanup are re-exported
from youtube.py — vkmusic imports the same canonical classes, so callers
don't care which source produced them.
"""

from __future__ import annotations

import logging
import re

import vkmusic
import youtube
from config import DISABLE_YOUTUBE_FALLBACK
from youtube import DownloadedTrack, FileTooLargeError, TrackMeta, cleanup  # noqa: F401  (re-export)

log = logging.getLogger(__name__)

# VK audio ids: optional minus, digits, underscore, digits — e.g. "12345_67890"
# or "-2000000000_67890" for groups. YouTube ids are 11 chars from the set
# [A-Za-z0-9_-] and never contain an underscore between two digit-runs at
# the bookends, so this regex disambiguates cleanly.
_VK_ID_RE = re.compile(r"^-?\d+_\d+$")


def _is_vk_id(video_id: str) -> bool:
    return bool(_VK_ID_RE.match(video_id))


async def search(query: str, limit: int) -> list[TrackMeta]:
    """Search VK first; fall through to YouTube if VK has no hits or no token.

    Returns at most `limit` items total (does not concatenate).
    """
    if vkmusic.is_configured():
        results = await vkmusic.search(query, limit)
        if results:
            return results
        log.info("VK returned 0 results for %r — falling back to YouTube", query)
    if DISABLE_YOUTUBE_FALLBACK:
        return []
    return await youtube.search(query, limit)


async def download(video_id: str) -> DownloadedTrack:
    """Route by id format. Raises if no source can handle it."""
    if _is_vk_id(video_id):
        if not vkmusic.is_configured():
            raise RuntimeError(
                f"VK audio id {video_id!r} received but VK_TOKEN is not set"
            )
        return await vkmusic.download(video_id)
    if DISABLE_YOUTUBE_FALLBACK:
        raise RuntimeError(
            f"YouTube id {video_id!r} but YouTube fallback is disabled"
        )
    return await youtube.download(video_id)
