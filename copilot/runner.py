"""Run the Telegram bot and the web dashboard in one process.

Railway bills per service, so both live in a single container. The bot owns
all writes; the dashboard only reads. If the web server dies the bot keeps
alerting, and vice versa — neither failure is silent.
"""
import asyncio
import logging

import uvicorn

from . import config
from .bot.app import build, close_market, notify_online

log = logging.getLogger(__name__)


async def _serve_web() -> None:
    from .web.server import app as web_app
    server = uvicorn.Server(uvicorn.Config(
        web_app, host="0.0.0.0", port=config.PORT,
        log_level="warning", access_log=False))
    log.info("dashboard listening on port %s", config.PORT)
    await server.serve()


async def _main() -> None:
    bot = build()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(allowed_updates=["message"])
    # post_init/post_shutdown are only invoked by run_polling(), so this path
    # has to do their work itself.
    await notify_online(bot)
    log.info("co-pilot running (bot + dashboard)")
    try:
        await _serve_web()          # blocks until the server stops
    finally:
        log.info("shutting down")
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()
        await close_market(bot)


def run_all() -> None:
    if not config.WEB_PASSWORD:
        log.warning("WEB_PASSWORD not set — dashboard will refuse all requests. "
                    "Set it in .env (or Railway variables) to enable.")
    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, SystemExit):
        pass
