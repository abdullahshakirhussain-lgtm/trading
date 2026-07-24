"""Wires everything together: Telegram Application + JobQueue pollers."""
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .. import config, db
from ..data.binance import Market
from ..engine import alerts
from . import commands

log = logging.getLogger(__name__)


def _send_factory(context: ContextTypes.DEFAULT_TYPE):
    """Async sender targeting the chat captured by /start."""
    async def send(text: str) -> None:
        chat_id = db.kv_get("alert_chat_id")
        if not chat_id:
            log.info("alert (no chat yet): %s", text.replace("\n", " | "))
            return
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=text,
                                           parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True)
        except Exception:
            log.exception("failed to send alert")
    return send


def _job(fn):
    """Adapt an alerts.<poller>(market, send) coroutine into a JobQueue callback."""
    async def callback(context: ContextTypes.DEFAULT_TYPE) -> None:
        market = context.application.bot_data["market"]
        try:
            await fn(market, _send_factory(context))
        except Exception:
            log.exception("job %s crashed", fn.__name__)
    callback.__name__ = fn.__name__
    return callback


async def _daily_brief_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    from ..llm import copilot
    if not copilot.enabled():
        return
    market = context.application.bot_data["market"]
    try:
        text = await copilot.daily_brief(market)
        await _send_factory(context)(text)
    except Exception:
        log.exception("daily brief failed")


async def _weekly_review_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    from ..llm import copilot
    if not copilot.enabled():
        return
    try:
        text = await copilot.weekly_review()
        await _send_factory(context)(text)
    except Exception:
        log.exception("weekly review failed")


async def notify_online(application: Application) -> None:
    chat_id = db.kv_get("alert_chat_id")
    if chat_id:
        try:
            await application.bot.send_message(
                chat_id=int(chat_id), text="🟢 co-pilot online")
        except Exception:
            log.warning("could not send online notice")


async def close_market(application: Application) -> None:
    market = application.bot_data.get("market")
    if market:
        await market.close()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error in handler/job", exc_info=context.error)


COMMANDS = {
    "start": commands.start, "help": commands.help_cmd,
    "price": commands.price, "funding": commands.funding,
    "movers": commands.movers, "fng": commands.fng,
    "watch": commands.watch, "unwatch": commands.unwatch,
    "watchlist": commands.watchlist_cmd,
    "alert": commands.alert, "alerts": commands.alerts_cmd,
    "delalert": commands.delalert,
    "radar": commands.radar, "news": commands.news_cmd, "heat": commands.heat,
    "read": commands.read_cmd,
    "buy": commands.buy, "sell": commands.sell, "close": commands.close,
    "positions": commands.positions_cmd, "stats": commands.stats,
    "brief": commands.brief, "check": commands.check, "review": commands.review,
    "status": commands.status,
}

# (poller, interval_s, first_delay_s) — staggered so startup isn't a burst
JOBS = [
    (alerts.poll_prices, config.PRICES_INTERVAL, 5),
    (alerts.poll_announcements, config.ANNOUNCEMENTS_INTERVAL, 10),
    (alerts.poll_funding, config.FUNDING_INTERVAL, 20),
    (alerts.poll_new_symbols, config.NEW_SYMBOLS_INTERVAL, 30),
    (alerts.poll_movers, config.MOVERS_INTERVAL, 45),
    (alerts.poll_news, config.NEWS_INTERVAL, 60),
    (alerts.poll_radar, config.RADAR_INTERVAL, 90),
    (alerts.poll_fng, config.FNG_INTERVAL, 120),
    (alerts.poll_volatility, config.VOL_INTERVAL, 300),
    (alerts.snapshot_equity, 3600, 150),
    (alerts.poll_conditions, 900, 180),
]


def build() -> Application:
    """Construct the Application with all handlers and jobs registered."""
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN missing. Create a bot with @BotFather, then put the "
            "token in .env (copy .env.example). Or run with --selftest (no Telegram needed).")
    db.init()
    app = (Application.builder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .post_init(notify_online)
           .post_shutdown(close_market)
           .build())
    # Created here, not in post_init: post_init only runs under run_polling(),
    # so building it there left every job with KeyError('market') in the
    # combined bot+web runner.
    app.bot_data["market"] = Market()
    app.add_error_handler(_on_error)
    for name, fn in COMMANDS.items():
        app.add_handler(CommandHandler(name, fn))
    for fn, interval, first in JOBS:
        app.job_queue.run_repeating(_job(fn), interval=interval, first=first,
                                    name=fn.__name__)
    tz = ZoneInfo(config.TIMEZONE)
    app.job_queue.run_daily(_job(alerts.daily_housekeeping),
                            time=dt.time(0, 5, tzinfo=tz), name="housekeeping")
    app.job_queue.run_daily(_daily_brief_job,
                            time=dt.time(config.BRIEF_HOUR, 0, tzinfo=tz), name="brief")
    app.job_queue.run_daily(_weekly_review_job, days=(0,),
                            time=dt.time(config.BRIEF_HOUR, 30, tzinfo=tz), name="review")
    return app


def run() -> None:
    """Bot only, no dashboard."""
    app = build()
    log.info("co-pilot starting (bot only, polling)")
    app.run_polling(allowed_updates=["message"])
