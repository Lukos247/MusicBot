"""VK Music — the bot's audio source.

Two auth paths are supported, picked at startup:

1. **Browser cookies** (preferred — bypasses password flow entirely).
   Set `VK_COOKIES_B64` + `VK_USER_ID`. The bot loads the cookies into
   a requests session, sets a browser User-Agent, and scrapes
   m.vk.com/audio via vk_api.audio.VkAudio. Works as long as the
   cookies are still valid in the browser they were exported from
   (typically weeks). Doesn't trigger VK's password-bruteforce flood
   control.

2. **Direct-auth Kate Mobile token** (legacy, often blocked by flood
   control). Set `VK_TOKEN`. Used only if cookies aren't configured.
   Talks to api.vk.com/method/audio.search directly. Token must come
   from `oauth.vk.com/token` (grant_type=password) — implicit-flow
   tokens from vkhost.github.io are rejected with `error 3`.

video_id format: VK's canonical `{owner_id}_{audio_id}` string —
e.g. `12345_67890` or `-2000000000_67890` for groups.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

import requests

from config import DOWNLOADS_DIR, MAX_AUDIO_BYTES, VK_TOKEN, VK_COOKIES_B64, VK_USER_ID

log = logging.getLogger(__name__)


class FileTooLargeError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"File too large: {size} bytes")
        self.size = size


@dataclass(slots=True)
class TrackMeta:
    video_id: str
    title: str
    performer: str
    duration: int


@dataclass(slots=True)
class DownloadedTrack:
    path: Path
    title: str
    performer: str
    duration: int


_VK_API_BASE = "https://api.vk.com/method"
_VK_API_VERSION = "5.131"
_KATE_UA = (
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; "
    "x86; unknown Android SDK built for x86; en)"
)
# Browser UA paired with browser-exported cookies — m.vk.com checks UA
# against the session and gives different responses for "looks like a
# bot vs. browser".
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _diag(msg: str) -> None:
    """Module-import-time visibility — log.* may be swallowed before
    main() runs basicConfig."""
    print(f"[vkmusic] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Cookies path: m.vk.com scraping via vk_api.audio.VkAudio
# ---------------------------------------------------------------------------

_audio_scraper = None  # vk_api.audio.VkAudio instance, or None


def _parse_netscape_cookies(text: str) -> dict[str, str]:
    """Minimal Netscape cookies.txt parser — returns name→value for
    cookies on .vk.com / vk.com / m.vk.com domains."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, _exp, name, value = parts[:7]
        if "vk.com" in domain:
            out[name] = value
    return out


def _build_audio_scraper():
    """Construct a vk_api.audio.VkAudio that uses an authenticated session
    derived from the user's browser cookies. Returns None on failure."""
    if not (VK_COOKIES_B64 and VK_USER_ID):
        return None
    try:
        import vk_api
        from vk_api.audio import VkAudio
    except ImportError:
        _diag("vk_api / beautifulsoup4 not installed — cookies path disabled")
        return None
    try:
        cookies_text = base64.b64decode(VK_COOKIES_B64).decode("utf-8")
    except Exception as e:
        _diag(f"VK_COOKIES_B64 decode failed: {e}")
        return None
    cookies = _parse_netscape_cookies(cookies_text)
    if not cookies:
        _diag("no .vk.com cookies parsed from VK_COOKIES_B64")
        return None
    try:
        user_id = int(VK_USER_ID)
    except ValueError:
        _diag(f"VK_USER_ID not a valid integer: {VK_USER_ID!r}")
        return None

    # Build a requests session with the cookies + a browser UA. m.vk.com
    # will treat it as the original logged-in browser.
    http = requests.Session()
    for name, value in cookies.items():
        http.cookies.set(name, value, domain=".vk.com", path="/")
    # Phase 1 headers — plain browser-like, no AJAX marker. We need this
    # for the warmup GET so m.vk.com performs an HTTP-level redirect to
    # login.vk.com (which exchanges the vk.com session for an m.vk.com
    # one). With X-Requested-With set the same call would respond with a
    # JSON `{location: ...}` body and no actual redirect chain — meaning
    # m.vk.com cookies would never get planted.
    http.headers.update({
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    })

    # vk_api.VkApi normally needs login/password or a token. We bypass
    # __init__'s auth entirely by setting attrs manually — VkAudio only
    # uses self._vk.http for its m.vk.com requests, never the API token.
    session = vk_api.VkApi.__new__(vk_api.VkApi)
    session.http = http
    session.token = None
    session.lock = None  # not used by VkAudio
    session.api_version = _VK_API_VERSION

    # Warmup: a vk.com session doesn't auto-share its session token with
    # m.vk.com. The first browser-like GET to m.vk.com/audio HTTP-302s
    # to login.vk.com?role=pda_frame, which exchanges our vk.com session
    # for an m.vk.com one and bounces back. requests' default
    # follow-redirects rides that chain and the cookie jar afterwards
    # carries the m.vk.com session.
    cookies_before = len(http.cookies)
    try:
        warm = http.get(
            "https://m.vk.com/audio", timeout=15, allow_redirects=True,
        )
        _diag(
            f"warmup: status={warm.status_code}, final_url={warm.url}, "
            f"cookies {cookies_before} → {len(http.cookies)}, "
            f"history={[h.status_code for h in warm.history]}"
        )
        if "login" in warm.url and "login.vk.com" not in warm.url:
            _diag("warmup ended on a login form — auth bridge didn't complete")
    except Exception as e:
        _diag(f"warmup failed: {e}")

    # Phase 2 headers — now that the session is m.vk.com-authenticated,
    # subsequent vk_api.audio.VkAudio POSTs need the AJAX markers so
    # m.vk.com returns the JSON envelope rather than a full HTML page.
    http.headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://m.vk.com",
        "Referer": "https://m.vk.com/audio",
    })

    audio = VkAudio.__new__(VkAudio)
    audio._vk = session
    audio.user_id = user_id
    audio.convert_m3u8_links = True
    _diag(
        f"VkAudio (cookies path) initialised: {len(cookies)} cookies in, "
        f"{len(http.cookies)} after warmup, user_id={user_id}"
    )
    return audio


# ---------------------------------------------------------------------------
# Token path: api.vk.com/method/audio.search
# ---------------------------------------------------------------------------


def _vk_call(method: str, **params: Any) -> dict | None:
    if not VK_TOKEN:
        return None
    full_params = dict(params)
    full_params["access_token"] = VK_TOKEN
    full_params["v"] = _VK_API_VERSION
    try:
        resp = requests.get(
            f"{_VK_API_BASE}/{method}",
            params=full_params,
            headers={"User-Agent": _KATE_UA},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("VK %s network error: %s", method, e)
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning("VK %s returned non-JSON: %s", method, resp.text[:200])
        return None
    if "error" in data:
        err = data["error"]
        log.warning("VK %s API error %s: %s", method, err.get("error_code"), err.get("error_msg"))
        return None
    return data.get("response")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    return bool(_audio_scraper) or bool(VK_TOKEN)


def _to_track_meta(item: dict) -> TrackMeta:
    return TrackMeta(
        video_id=f"{item['owner_id']}_{item['id']}",
        title=(item.get("title") or "").strip() or "Unknown",
        performer=(item.get("artist") or "").strip() or "Unknown",
        duration=int(item.get("duration") or 0),
    )


def _probe_mvk_response(query: str) -> None:
    """Hit m.vk.com/audio directly with the configured cookies and log
    the raw shape so we can see what's actually wrong when vk_api crashes."""
    if _audio_scraper is None:
        return
    http = _audio_scraper._vk.http
    try:
        r = http.post(
            "https://m.vk.com/audio",
            data={
                "act": "section",
                "al": 1,
                "claim": 0,
                "is_layer": 0,
                "owner_id": _audio_scraper.user_id,
                "section": "search",
                "q": query,
            },
            timeout=15,
            allow_redirects=False,
        )
    except Exception as e:
        log.warning("m.vk.com probe network error: %s", e)
        return
    body = r.text or ""
    head = body[:400].replace("\n", " ")
    log.warning(
        "m.vk.com probe: status=%s, final_url=%s, body_len=%d, head=%r",
        r.status_code, r.url, len(body), head,
    )


def _search_blocking(query: str, limit: int) -> list[TrackMeta]:
    if _audio_scraper is not None:
        try:
            results = list(islice(_audio_scraper.search(q=query, count=limit), limit))
        except Exception:
            log.exception("m.vk.com search failed for %r", query)
            _probe_mvk_response(query)
            return []
        log.info("vk cookies-search %r → %d items", query, len(results))
        return [_to_track_meta(item) for item in results]

    # Token fallback
    response = _vk_call("audio.search", q=query, count=limit, auto_complete=1)
    if response is None:
        return []
    items = response.get("items") or []
    log.info("vk audio.search %r → %d items", query, len(items))
    return [_to_track_meta(item) for item in islice(items, limit)]


async def search(query: str, limit: int) -> list[TrackMeta]:
    query = query.strip()
    if not query:
        return []
    return await asyncio.to_thread(_search_blocking, query, limit)


_VIDEO_ID_RE = re.compile(r"^(-?\d+)_(\d+)$")


def _parse_video_id(video_id: str) -> tuple[int, int] | None:
    m = _VIDEO_ID_RE.match(video_id)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _fetch_audio(owner_id: int, audio_id: int) -> dict | None:
    if _audio_scraper is not None:
        try:
            item = _audio_scraper.get_audio_by_id(owner_id, audio_id)
        except Exception:
            log.exception("m.vk.com get_audio_by_id failed for %s_%s", owner_id, audio_id)
            return None
        if isinstance(item, dict):
            return item
        if isinstance(item, list) and item:
            return item[0]
        return None

    # Token fallback
    response = _vk_call("audio.getById", audios=f"{owner_id}_{audio_id}")
    if response is None:
        return None
    if isinstance(response, list):
        return response[0] if response else None
    return response


def _download_blocking(video_id: str) -> DownloadedTrack:
    parsed = _parse_video_id(video_id)
    if parsed is None:
        raise RuntimeError(f"Bad VK audio id: {video_id!r}")
    owner_id, audio_id = parsed

    item = _fetch_audio(owner_id, audio_id)
    if not item:
        raise RuntimeError(f"VK audio not found: {video_id}")

    url = item.get("url")
    if not url:
        raise RuntimeError(f"VK audio has no playable URL: {video_id}")

    title = (item.get("title") or "").strip() or "Unknown"
    artist = (item.get("artist") or "").strip() or "Unknown"
    duration = int(item.get("duration") or 0)

    output = DOWNLOADS_DIR / f"vk_{owner_id}_{audio_id}.m4a"
    if output.exists():
        try:
            output.unlink()
        except OSError:
            pass

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-nostdin",
        "-i", url,
        "-vn",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timed out downloading {video_id}")

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:400].strip()
        raise RuntimeError(f"ffmpeg failed for {video_id}: {err}")
    if not output.exists():
        raise RuntimeError(f"ffmpeg ran but output missing for {video_id}")

    size = output.stat().st_size
    if size > MAX_AUDIO_BYTES:
        try:
            output.unlink()
        except OSError:
            pass
        raise FileTooLargeError(size)

    return DownloadedTrack(
        path=output,
        title=title,
        performer=artist,
        duration=duration,
    )


async def download(video_id: str) -> DownloadedTrack:
    return await asyncio.to_thread(_download_blocking, video_id)


def cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Failed to remove file %s", path)


# ---------------------------------------------------------------------------
# Module-load init
# ---------------------------------------------------------------------------

_audio_scraper = _build_audio_scraper()
if _audio_scraper is not None:
    _diag("primary path: cookies-based m.vk.com scraping")
elif VK_TOKEN:
    _diag(f"primary path: api.vk.com/audio.search (VK_TOKEN, {len(VK_TOKEN)} chars)")
else:
    _diag("no auth configured — vkmusic disabled")
