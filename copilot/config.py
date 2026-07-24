"""Central configuration. Values come from .env with sensible defaults.

Everything here is tunable without touching code — put overrides in .env.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


# --- Secrets / integrations ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

# --- Storage / logging ---
# DATA_DIR is overridable so Railway can point it at a mounted volume (/data).
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "copilot.db"
LOG_DIR = Path(os.getenv("LOG_DIR", PROJECT_ROOT / "logs"))

# --- Web dashboard ---
PORT = _i("PORT", 8000)                      # Railway injects PORT
WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "").strip()  # empty = dashboard disabled

# --- Locale ---
TIMEZONE = os.getenv("TIMEZONE", "Asia/Colombo")
BRIEF_HOUR = _i("BRIEF_HOUR", 8)  # local hour for the daily LLM brief

# --- Watchlist (seed; managed at runtime via /watch) ---
DEFAULT_WATCHLIST = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# --- Poll intervals (seconds) ---
PRICES_INTERVAL = _i("PRICES_INTERVAL", 120)
FUNDING_INTERVAL = _i("FUNDING_INTERVAL", 600)
MOVERS_INTERVAL = _i("MOVERS_INTERVAL", 600)
FNG_INTERVAL = _i("FNG_INTERVAL", 3600)
NEWS_INTERVAL = _i("NEWS_INTERVAL", 900)
ANNOUNCEMENTS_INTERVAL = _i("ANNOUNCEMENTS_INTERVAL", 90)
NEW_SYMBOLS_INTERVAL = _i("NEW_SYMBOLS_INTERVAL", 300)
RADAR_INTERVAL = _i("RADAR_INTERVAL", 600)
VOL_INTERVAL = _i("VOL_INTERVAL", 900)

# --- Alert thresholds ---
FUNDING_EXTREME = _f("FUNDING_EXTREME", 0.0005)   # |0.05%| per 8h funding = crowded
WATCHLIST_MOVE_PCT = _f("WATCHLIST_MOVE_PCT", 10)  # 24h % move on a watched coin
MARKET_MOVER_PCT = _f("MARKET_MOVER_PCT", 20)      # 24h % move market-wide
MOVER_MIN_QVOL = _f("MOVER_MIN_QVOL", 5_000_000)   # min 24h quote volume (USD) to count
FNG_LOW = _i("FNG_LOW", 20)
FNG_HIGH = _i("FNG_HIGH", 80)
VOL_SPIKE_RATIO = _f("VOL_SPIKE_RATIO", 2.0)       # 24h realized vol vs 14d baseline

# --- Memecoin radar / rug filter ---
RADAR_CHAINS = [c.strip() for c in os.getenv("RADAR_CHAINS", "solana,bsc").split(",") if c.strip()]
RUG_MIN_LIQ_USD = _f("RUG_MIN_LIQ_USD", 25_000)
RUG_MIN_AGE_H = _f("RUG_MIN_AGE_H", 24)
RUG_MIN_VOL_LIQ = _f("RUG_MIN_VOL_LIQ", 0.2)   # vol24/liquidity below this = dead
RUG_MAX_VOL_LIQ = _f("RUG_MAX_VOL_LIQ", 50)    # above this = likely wash trading

# --- Paper trading ---
PAPER_START_CASH = _f("PAPER_START_CASH", 10_000)
FEE_RATE = _f("FEE_RATE", 0.001)               # Binance taker 0.1%
SLIPPAGE_CORE = _f("SLIPPAGE_CORE", 0.0005)    # majors
SLIPPAGE_DEGEN = _f("SLIPPAGE_DEGEN", 0.01)    # low-liquidity memes: 1%
DEGEN_CAP = _f("DEGEN_CAP", 0.20)              # max share of equity in degen cost basis

# --- Alert dedup cooldowns (seconds) ---
COOLDOWN_FUNDING = _i("COOLDOWN_FUNDING", 8 * 3600)
COOLDOWN_MOVER = _i("COOLDOWN_MOVER", 12 * 3600)
COOLDOWN_VOL = _i("COOLDOWN_VOL", 12 * 3600)
COOLDOWN_FNG = _i("COOLDOWN_FNG", 12 * 3600)
COOLDOWN_HEAT = _i("COOLDOWN_HEAT", 12 * 3600)

# --- News feeds (all free) ---
NEWS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "TheBlock": "https://www.theblock.co/rss.xml",
}

# --- Narrative heat ---
HEAT_MIN_COUNT = _i("HEAT_MIN_COUNT", 5)       # min mentions in 24h to consider
HEAT_ACCEL_RATIO = _f("HEAT_ACCEL_RATIO", 3.0)  # vs prior 7d daily average
