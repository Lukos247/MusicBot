from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiohttp import web

import cache
from config import BOT_TOKEN
from handlers import build_router


async def _run_health_server() -> web.AppRunner:
    """Tiny HTTP server so PaaS health checks (Fly.io, Render, Railway, etc.)
    have something to talk to. The bot itself uses long-polling and does not
    need inbound HTTP — this exists purely to satisfy the platform proxy."""
    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Health server listening on 0.0.0.0:%d", port)
    return runner


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    await cache.init_db()

    # Start the health server first so the platform proxy has something to
    # connect to before we make any outbound calls (bot.get_me() etc.).
    health_runner = await _run_health_server()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(build_router())

    me = await bot.get_me()
    logging.info("Bot started @%s (id=%s)", me.username, me.id)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await health_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down")
        sys.exit(0)
