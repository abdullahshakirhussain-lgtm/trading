"""SQLite storage. Single file, thread-safe via a module lock.

All writes go through execute(); calls are sub-millisecond so we keep them
synchronous even inside async code.
"""
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS prices (ts INTEGER, symbol TEXT, price REAL);
CREATE INDEX IF NOT EXISTS idx_prices ON prices(symbol, ts);
CREATE TABLE IF NOT EXISTS funding (ts INTEGER, symbol TEXT, rate REAL);
CREATE INDEX IF NOT EXISTS idx_funding ON funding(symbol, ts);
CREATE TABLE IF NOT EXISTS price_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, direction TEXT, level REAL,
    created_ts INTEGER, fired_ts INTEGER
);
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, kind TEXT, dedup_key TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_dedup ON alert_log(dedup_key, ts);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, ts INTEGER, source TEXT, title TEXT, url TEXT,
    narratives TEXT, severity TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);
CREATE TABLE IF NOT EXISTS known_symbols (
    market TEXT, symbol TEXT, first_seen INTEGER,
    PRIMARY KEY (market, symbol)
);
CREATE TABLE IF NOT EXISTS announcements (id TEXT PRIMARY KEY, ts INTEGER, title TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS radar (
    chain TEXT, token_addr TEXT, first_seen INTEGER, last_seen INTEGER,
    symbol TEXT, name TEXT, verdict TEXT, reasons TEXT,
    liq_usd REAL, vol24 REAL, age_h REAL, price_usd REAL, url TEXT,
    PRIMARY KEY (chain, token_addr)
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, symbol TEXT, side TEXT, usd REAL, qty REAL, price REAL,
    fee REAL, bucket TEXT, is_real INTEGER DEFAULT 0, realized_pnl REAL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT, bucket TEXT, is_real INTEGER,
    qty REAL, avg_cost REAL,
    PRIMARY KEY (symbol, bucket, is_real)
);
CREATE TABLE IF NOT EXISTS equity_history (ts INTEGER, equity REAL, btc_bench REAL);
"""


def init() -> None:
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()
    # Seed watchlist on first run only
    if not fetchall("SELECT 1 FROM watchlist LIMIT 1"):
        for sym in config.DEFAULT_WATCHLIST:
            execute("INSERT OR IGNORE INTO watchlist(symbol) VALUES (?)", (sym,))


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def executemany(sql: str, rows: list[tuple]) -> None:
    with _lock:
        _conn.executemany(sql, rows)
        _conn.commit()


def fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, params).fetchall()


def fetchone(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return _conn.execute(sql, params).fetchone()


# --- kv helpers ---

def kv_get(key: str, default: str | None = None) -> str | None:
    row = fetchone("SELECT v FROM kv WHERE k = ?", (key,))
    return row["v"] if row else default


def kv_set(key: str, value: str) -> None:
    execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, str(value)))


# --- watchlist ---

def watchlist() -> list[str]:
    return [r["symbol"] for r in fetchall("SELECT symbol FROM watchlist ORDER BY symbol")]


# --- alert dedup: True if this key hasn't fired within cooldown ---

def dedup_ok(kind: str, key: str, cooldown_s: int) -> bool:
    cutoff = int(time.time()) - cooldown_s
    row = fetchone(
        "SELECT 1 FROM alert_log WHERE kind = ? AND dedup_key = ? AND ts > ? LIMIT 1",
        (kind, key, cutoff))
    return row is None


def log_alert(kind: str, key: str, message: str) -> None:
    execute("INSERT INTO alert_log(ts, kind, dedup_key, message) VALUES (?, ?, ?, ?)",
            (int(time.time()), kind, key, message))


def prune(days: int = 45) -> None:
    """Keep the DB small: drop old time-series rows."""
    cutoff = int(time.time()) - days * 86400
    execute("DELETE FROM prices WHERE ts < ?", (cutoff,))
    execute("DELETE FROM funding WHERE ts < ?", (cutoff,))
    execute("DELETE FROM alert_log WHERE ts < ?", (cutoff,))
    execute("DELETE FROM news WHERE ts < ?", (cutoff,))
