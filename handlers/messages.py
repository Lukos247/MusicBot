from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import vkmusic
from config import SEARCH_LIMIT_DM

from .common import deliver_track

router = Router(name="messages")
log = logging.getLogger(__name__)


def _format_button_label(track: vkmusic.TrackMeta) -> str:
    label = f"{track.performer} — {track.title}" if track.performer else track.title
    if track.duration:
        m, s = divmod(track.duration, 60)
        label += f"  ({m}:{s:02d})"
    if len(label) > 64:
        label = label[:61] + "…"
    return label


def _build_keyboard(tracks: list[vkmusic.TrackMeta]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_format_button_label(t), callback_data=f"pick:{t.video_id}")]
        for t in tracks
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, bot: Bot) -> None:
    arg = (command.args or "").strip()
    if arg.startswith("dl_"):
        video_id = arg[3:]
        if video_id:
            status = await message.answer("⏳ Скачиваю трек…")
            await deliver_track(bot, message.chat.id, video_id, status_msg=status)
            return
    await _send_greeting(message, bot)


async def _send_greeting(message: Message, bot: Bot) -> None:
    me = await bot.me()
    await message.answer(
        "Привет! Напиши название трека — найду и пришлю аудио.\n\n"
        f"Также можешь использовать меня в любом чате: набери "
        f"@{me.username} название и выбери результат."
    )


@router.message(F.text & ~F.text.startswith("/"))
async def search_text(message: Message) -> None:
    query = (message.text or "").strip()
    if not query:
        return

    status = await message.answer("🔎 Ищу…")
    tracks = await vkmusic.search(query, SEARCH_LIMIT_DM)
    if not tracks:
        await status.edit_text("Ничего не нашлось. Попробуй уточнить запрос.")
        return

    keyboard = _build_keyboard(tracks)
    await status.edit_text("Выбери трек:", reply_markup=keyboard)
