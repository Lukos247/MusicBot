from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InputMediaAudio,
    InputTextMessageContent,
)

import cache
import youtube
from config import INLINE_CACHE_TIME, INLINE_SEARCH_TIMEOUT, SEARCH_LIMIT_INLINE

from .common import materialize_file_id
from youtube import FileTooLargeError

router = Router(name="inline")
log = logging.getLogger(__name__)


async def _safe_answer(query: InlineQuery, **kwargs) -> None:
    """Telegram drops inline answers if the query is older than ~10 seconds.
    The error is harmless — just swallow it instead of dumping a traceback."""
    try:
        await query.answer(**kwargs)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "query is too old" in msg or "query id is invalid" in msg:
            return
        raise


def _format_duration(seconds: int) -> str:
    if not seconds:
        return ""
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _make_article(t: youtube.TrackMeta) -> InlineQueryResultArticle:
    """Build an Article result for a non-cached track. The Article posts a
    text placeholder; the chosen_inline_result handler later edits it into
    audio via editMessageMedia.

    A reply_markup is mandatory — without it Telegram does not return
    inline_message_id in chosen_inline_result, and the edit cannot happen.
    """
    duration = _format_duration(t.duration)
    if t.performer and duration:
        description = f"{t.performer} · {duration}"
    elif t.performer:
        description = t.performer
    elif duration:
        description = duration
    else:
        description = "YouTube"

    title_html = html.escape(t.title or "Unknown")
    body = f"🎵 <b>{title_html}</b>"
    if t.performer:
        body += f" — {html.escape(t.performer)}"
    body += "\n⏳ Скачиваю..."

    return InlineQueryResultArticle(
        id=t.video_id,
        title=t.title or "Unknown",
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=body,
            parse_mode="HTML",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏳ Загрузка…", callback_data="loading")
        ]]),
        thumbnail_url=f"https://i.ytimg.com/vi/{t.video_id}/mqdefault.jpg",
        thumbnail_width=320,
        thumbnail_height=180,
    )


@router.inline_query()
async def on_inline_query(query: InlineQuery, bot: Bot) -> None:
    text = (query.query or "").strip()
    if not text:
        await _safe_answer(
            query,
            results=[],
            cache_time=1,
            is_personal=True,
            switch_pm_text="Введи название трека…",
            switch_pm_parameter="help",
        )
        return

    cached_tracks = await cache.search_text(text, SEARCH_LIMIT_INLINE)
    cached_ids = {t.video_id for t in cached_tracks}
    cached_results: list = [
        InlineQueryResultCachedAudio(
            id=t.video_id,
            audio_file_id=t.file_id,
        )
        for t in cached_tracks
    ]

    fresh_results: list[InlineQueryResultArticle] = []
    if len(cached_results) < SEARCH_LIMIT_INLINE:
        need = SEARCH_LIMIT_INLINE - len(cached_results)
        try:
            yt_tracks = await asyncio.wait_for(
                youtube.search(text, SEARCH_LIMIT_INLINE),
                timeout=INLINE_SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.info("youtube.search timed out for %r", text)
            yt_tracks = []
        for t in yt_tracks:
            if t.video_id in cached_ids:
                continue
            fresh_results.append(_make_article(t))
            if len(fresh_results) >= need:
                break

    results = cached_results + fresh_results
    if not results:
        preview = text if len(text) <= 25 else text[:24] + "…"
        await _safe_answer(
            query,
            results=[],
            cache_time=INLINE_CACHE_TIME,
            is_personal=True,
            switch_pm_text=f"Нет результатов для «{preview}»",
            switch_pm_parameter="help",
        )
        return

    # cache_time=0 when fresh Article placeholders are present, otherwise
    # Telegram serves the "⏳ Скачиваю..." stub to the next inline_query
    # for INLINE_CACHE_TIME seconds even after the audio has materialized.
    cache_time = 0 if fresh_results else INLINE_CACHE_TIME
    await _safe_answer(
        query,
        results=results,
        cache_time=cache_time,
        is_personal=True,
    )


async def _try_edit_inline_text(bot: Bot, inline_message_id: str, text: str) -> None:
    try:
        await bot.edit_message_text(inline_message_id=inline_message_id, text=text)
    except TelegramBadRequest:
        pass


@router.chosen_inline_result()
async def on_chosen_inline_result(chosen: ChosenInlineResult, bot: Bot) -> None:
    """Fires when the user picks an inline result. Required: BotFather
    Inline Feedback set to Enabled (100%).

    For cached audio results inline_message_id is None — Telegram already
    posted the audio, nothing to do.

    For Article results we received earlier inline_message_id is present;
    download the track, upload to STORAGE_CHAT_ID to get a file_id, and
    edit the placeholder text into an audio message via editMessageMedia.
    """
    video_id = chosen.result_id
    imid = chosen.inline_message_id
    if imid is None:
        log.info("chosen %s — cached audio, no action needed", video_id)
        return

    log.info("chosen %s — materializing", video_id)
    try:
        cached = await materialize_file_id(bot, video_id)
    except FileTooLargeError as e:
        size_mb = e.size // (1024 * 1024)
        await _try_edit_inline_text(
            bot, imid, f"❌ Файл слишком большой ({size_mb} MB, лимит 50 MB)."
        )
        return

    if cached is None:
        await _try_edit_inline_text(bot, imid, "❌ Не удалось скачать трек.")
        return

    try:
        await bot.edit_message_media(
            inline_message_id=imid,
            media=InputMediaAudio(
                media=cached.file_id,
                title=cached.title or None,
                performer=cached.performer or None,
                duration=cached.duration or None,
            ),
        )
    except TelegramBadRequest as e:
        log.warning("edit_message_media failed for %s (%s): %s", video_id, imid, e)
        await _try_edit_inline_text(bot, imid, "❌ Не удалось вставить аудио.")
