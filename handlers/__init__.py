from aiogram import Router

from . import callbacks, inline, messages


def build_router() -> Router:
    router = Router(name="main")
    router.include_router(messages.router)
    router.include_router(callbacks.router)
    router.include_router(inline.router)
    return router
