"""Formatting helpers for Telegram messages (HTML parse mode)."""
import html


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def sym(s: str) -> str:
    """Render a ccxt perp symbol the way Binance Futures shows it.

    'BTC/USDT:USDT' -> 'BTCUSDT'; 'BTC/USDT' -> 'BTCUSDT'; already-clean passes through.
    """
    if not s:
        return "?"
    return str(s).split(":")[0].replace("/", "")


def money(x: float) -> str:
    if x is None:
        return "?"
    if abs(x) >= 1000:
        return f"${x:,.0f}"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.6g}"


def price(x: float) -> str:
    if x is None:
        return "?"
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 1:
        return f"{x:,.3f}"
    return f"{x:.6g}"


def pct(x: float, digits: int = 1) -> str:
    if x is None:
        return "?"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.{digits}f}%"


def funding_pct(rate: float) -> str:
    """Funding rate as % per 8h period."""
    return f"{rate * 100:+.4f}%"


def compact_usd(x: float) -> str:
    if x is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:.1f}{unit}"
    return f"${x:.0f}"
