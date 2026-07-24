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

# --- Explosive-mover scanner (small/new-cap day-trading radar) ---
# Scans Binance spot + USD-M perps for igniting moves the majors watchlist and
# the DEX radar both miss. Keyless. Two stages: a cheap market-wide ticker screen,
# then OHLCV confirmation on the candidates. See engine/scanner.py.
SCAN_INTERVAL = _i("SCAN_INTERVAL", 150)          # seconds between full scans (~2.5m)
SCAN_SHORT_WINDOW_S = _i("SCAN_SHORT_WINDOW_S", 300)  # short-window delta target (5m)
SCAN_CANDIDATE_CAP = _i("SCAN_CANDIDATE_CAP", 20)  # max OHLCV lookups per scan (rate limit)
SCAN_RVOL = _f("SCAN_RVOL", 3.0)                   # latest 5m volume vs trailing median

# Universe: tradability floor + tier bands, by 24h quote volume (USD). Above
# SCAN_MID_MAX a coin is mainstream and left to the watchlist; below the floor
# it's untradeable. Tier is a label only — all tiers are scanned and flagged.
SCAN_MIN_QVOL = _f("SCAN_MIN_QVOL", 500_000)       # micro lower bound / hard floor
SCAN_SMALL_MIN = _f("SCAN_SMALL_MIN", 15_000_000)  # micro -> small boundary
SCAN_MID_MIN = _f("SCAN_MID_MIN", 75_000_000)      # small -> mid boundary
# Upper guard only drops true mega-caps that slipped the name exclude list. Set high
# on purpose: a viral small-cap on its explosive day can spike past $500M of volume —
# that's exactly when we want it — while BTC/ETH-scale coins sit in the billions.
SCAN_MID_MAX = _f("SCAN_MID_MAX", 3_000_000_000)

# Per-tier trigger thresholds. Micro moves more, so it needs a bigger jump to matter;
# mid is heavier and a smaller move is already notable. Aggressive defaults.
SCAN_MOM_5M = {"micro": _f("SCAN_MOM_5M_MICRO", 4.0),
               "small": _f("SCAN_MOM_5M_SMALL", 3.0),
               "mid":   _f("SCAN_MOM_5M_MID", 2.0)}      # % over ~5 min
SCAN_MOM_15M = {"micro": _f("SCAN_MOM_15M_MICRO", 8.0),
                "small": _f("SCAN_MOM_15M_SMALL", 6.0),
                "mid":   _f("SCAN_MOM_15M_MID", 4.0)}     # % over ~15 min
SCAN_CHG24 = {"micro": _f("SCAN_CHG24_MICRO", 25.0),
              "small": _f("SCAN_CHG24_SMALL", 20.0),
              "mid":   _f("SCAN_CHG24_MID", 15.0)}        # 24h % fallback trigger

# Fresh listings move hardest — they trigger at a fraction of the usual bar.
SCAN_NEW_LISTING_BOOST_H = _f("SCAN_NEW_LISTING_BOOST_H", 72)
SCAN_NEW_LISTING_FACTOR = _f("SCAN_NEW_LISTING_FACTOR", 0.6)

# Bases never worth scanning: majors (belong on the watchlist) and stablecoins.
# Leveraged tokens (…UP/DOWN, 3L/3S, BULL/BEAR) are pattern-matched in the scanner.
SCAN_EXCLUDE_BASES = set(b.strip().upper() for b in os.getenv(
    "SCAN_EXCLUDE_BASES",
    "BTC,ETH,BNB,SOL,XRP,DOGE,ADA,TRX,AVAX,LINK,DOT,LTC,BCH,MATIC,"
    "USDC,FDUSD,TUSD,DAI,USDP,USDD,EUR,USD1"
).split(",") if b.strip())

# --- Alert dedup cooldowns (seconds) ---
COOLDOWN_FUNDING = _i("COOLDOWN_FUNDING", 8 * 3600)
COOLDOWN_MOVER = _i("COOLDOWN_MOVER", 12 * 3600)
COOLDOWN_VOL = _i("COOLDOWN_VOL", 12 * 3600)
COOLDOWN_FNG = _i("COOLDOWN_FNG", 12 * 3600)
COOLDOWN_HEAT = _i("COOLDOWN_HEAT", 12 * 3600)
COOLDOWN_SCAN = _i("COOLDOWN_SCAN", 2 * 3600)  # per symbol+direction; aggressive = short

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
