"""crypto co-pilot entry point.

    python run.py             # start the Telegram bot + pollers (needs TELEGRAM_BOT_TOKEN)
    python run.py --selftest  # run every data module once and print results (no Telegram)
"""
import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from copilot import config, db


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(config.LOG_DIR / "copilot.log", maxBytes=2_000_000,
                            backupCount=3, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)


async def selftest() -> int:
    """Exercise every data source once. Returns count of failed checks."""
    from copilot.data import announcements, dexscreener, feargreed, news
    from copilot.data.binance import Market
    from copilot.engine import narrative, rugfilter, scanner

    db.init()
    market = Market()
    failures = 0

    async def check(name, coro, describe):
        nonlocal failures
        try:
            result = await coro
            print(f"[OK]   {name}: {describe(result)}")
            return result
        except Exception as e:
            failures += 1
            print(f"[FAIL] {name}: {e}")
            return None

    try:
        prices = await check("binance prices", market.watchlist_prices(),
                             lambda r: ", ".join(f"{s}={p:g}" for s, p in sorted(r.items())) or "EMPTY")
        if prices is not None and not prices:
            failures += 1
            print("[FAIL] binance prices: empty result")

        rates = await check("binance funding", market.funding_rates(),
                            lambda r: f"{len(r)} perps (BTC/USDT:USDT="
                                      f"{r.get('BTC/USDT:USDT', '?')})")
        _ = rates

        await check("binance movers", market.movers(config.MOVER_MIN_QVOL),
                    lambda r: f"{len(r)} pairs, top: {r[0]['symbol']} {r[0]['pct24h']:+.1f}%"
                              if r else "EMPTY")

        await check("fear&greed", feargreed.fetch(),
                    lambda r: f"{r['value']} ({r['label']})" if r else "unavailable")

        await check("binance announcements", announcements.fetch_latest(),
                    lambda r: f"{len(r)} articles, latest: {r[0]['title'][:60]}"
                              if r else "unavailable (CMS geo-blocked? exchangeInfo diff still covers listings)")

        spot = await check("exchangeInfo spot symbols", market.list_symbols("spot"),
                           lambda r: f"{len(r)} active symbols")
        _ = spot

        scan_rows = await check("mover scanner (spot+perps)", scanner.scan(market),
                                lambda r: f"{sum(1 for x in r if x['verdict'] == 'PASS')} PASS "
                                          f"of {len(r)} candidates")
        if scan_rows:
            print("       top movers (tier · 5m/15m/1h · rvol · verdict):")
            for h in sorted(scan_rows, key=lambda x: x["score"], reverse=True)[:6]:
                def _p(k, _h=h):
                    return f"{_h[k]:+.1f}%" if _h.get(k) is not None else "—"
                rv = f"{h['rvol']:.1f}x" if h.get("rvol") is not None else "—"
                new = " NEW" if h.get("is_new") else ""
                print(f"       [{h['verdict']}] {h['base']:<9} {h['market']:<4} {h['tier']:<5} "
                      f"{_p('mom_5m')}/{_p('mom_15m')}/{_p('mom_1h')} rvol={rv} "
                      f"vol=${h['qvol']:,.0f}{new}")

        candidates = await check("dexscreener trending",
                                 dexscreener.trending_candidates(config.RADAR_CHAINS),
                                 lambda r: f"{len(r)} candidates on {'/'.join(config.RADAR_CHAINS)}")
        if candidates:
            chain = candidates[0]["chain"]
            addrs = [c["addr"] for c in candidates if c["chain"] == chain][:10]
            pairs = await check("dexscreener pair stats", dexscreener.pair_stats(chain, addrs),
                                lambda r: f"{len(r)} pairs with data")
            if pairs:
                print("       radar sample with rug-filter verdicts:")
                for p in pairs[:5]:
                    verdict, reasons = rugfilter.evaluate(p)
                    why = f" — {'; '.join(reasons)}" if reasons else ""
                    print(f"       [{verdict}] {p['symbol']} liq=${p['liq_usd']:,.0f} "
                          f"vol24=${p['vol24']:,.0f}{why}")

        items = await check("news RSS ingest", news.poll(),
                            lambda r: f"{len(r)} new items stored")
        _ = items
        heat = narrative.heat()
        active = [h for h in heat if h["count24h"] > 0][:5]
        print(f"[OK]   narrative heat: " +
              (", ".join(f"{h['narrative']}={h['count24h']}" for h in active) or "no data yet"))

        n_news = db.fetchone("SELECT COUNT(*) AS n FROM news")["n"]
        print(f"[OK]   sqlite: {config.DB_PATH} ({n_news} news rows)")

        from copilot.llm import copilot as llm
        print(f"[--]   LLM module: {'ENABLED (' + config.ANTHROPIC_MODEL + ')' if llm.enabled() else 'disabled (no ANTHROPIC_API_KEY)'}")
        print(f"[--]   Telegram: {'token set' if config.TELEGRAM_BOT_TOKEN else 'NO TOKEN — bot mode unavailable'}")
    finally:
        await market.close()

    print(f"\nselftest {'PASSED' if failures == 0 else f'FAILED ({failures} failures)'}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="crypto co-pilot")
    parser.add_argument("--selftest", action="store_true",
                        help="run all data modules once, no Telegram needed")
    parser.add_argument("--bot-only", action="store_true",
                        help="Telegram bot without the web dashboard")
    parser.add_argument("--web-only", action="store_true",
                        help="dashboard without the bot (read-only view)")
    args = parser.parse_args()
    setup_logging()
    if args.selftest:
        sys.exit(1 if asyncio.run(selftest()) else 0)
    if args.bot_only:
        from copilot.bot.app import run
        run()
    elif args.web_only:
        from copilot import db
        from copilot.runner import _serve_web
        db.init()
        asyncio.run(_serve_web())
    else:
        from copilot.runner import run_all
        run_all()


if __name__ == "__main__":
    main()
