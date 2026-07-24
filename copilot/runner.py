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


def _startup_report() -> None:
    """Say plainly what is and isn't configured — a silent misconfig on a remote
    host is near-impossible to diagnose from a failed healthcheck alone."""
    log.info("config: PORT=%s DATA_DIR=%s", config.PORT, config.DATA_DIR)
    for name, present, effect in (
        ("TELEGRAM_BOT_TOKEN", bool(config.TELEGRAM_BOT_TOKEN), "no alerts, no commands"),
        ("WEB_PASSWORD", bool(config.WEB_PASSWORD), "dashboard refuses all requests"),
        ("ANTHROPIC_API_KEY", bool(config.ANTHROPIC_API_KEY), "/brief /check /review disabled"),
    ):
        log.info("config: %-19s %s%s", name, "set" if present else "MISSING",
                 "" if present else f" -> {effect}")


async def _start_bot():
    """Start the bot. Never lets a bot failure take down the web server."""
    try:
        bot = build()
        await bot.initialize()
        await bot.start()
        await bot.updater.start_polling(allowed_updates=["message"])
        # post_init/post_shutdown only run under run_polling(), so do their work here.
        await notify_online(bot)
        log.info("telegram bot polling")
        return bot
    except (Exception, SystemExit):
        log.exception("BOT FAILED TO START - dashboard stays up, but no alerts "
                      "will be sent. Check TELEGRAM_BOT_TOKEN.")
        return None


async def _main() -> None:
    _startup_report()
    # Web server first: the healthcheck and dashboard must not depend on Telegram.
    # Starting the bot first meant a bad token produced a container that never
    # bound a port, which reads as an opaque "healthcheck failed" on Railway.
    web_task = asyncio.create_task(_serve_web())
    await asyncio.sleep(0)          # let uvicorn bind before anything can block
    bot = await _start_bot()
    log.info("co-pilot running (dashboard%s)", " + bot" if bot else ", NO BOT")
    try:
        await web_task              # blocks until the server stops
    finally:
        log.info("shutting down")
        if bot:
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
