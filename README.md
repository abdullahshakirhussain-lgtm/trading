# crypto co-pilot

Personal crypto market co-pilot: **alerts + analysis only, never execution.**
Watches Binance, memecoin launches, and crypto news; sends Telegram alerts;
maintains a paper-trading track record (fake $10k, core vs degen buckets).
Runs locally on Windows for ~$0/month.

## What it does

- **Watchlist alerts** — price crosses, funding-rate extremes, volatility regime
  changes, big movers, Fear & Greed extremes
- **Listing radar** — Binance new-listing announcements (polled every 90s;
  minutes-sensitive) + reliable new-symbol detection via exchangeInfo diff
- **Explosive-mover scanner** — Binance spot **+ USD-M perps** swept every ~2.5 min
  for small/new-cap coins igniting *now*: short-window momentum (5m/15m/1h) backed by
  a volume surge (RVOL), tiered micro/small/mid, with fresh listings on a lower bar.
  This is the layer that catches the ZAMA/UAI-type day-trading movers the majors
  watchlist and the DEX radar both miss. Pushed to Telegram in real time; `/hot` on demand
- **Memecoin radar** — DexScreener trending/boosted tokens on Solana + BSC,
  each run through a rug filter (liquidity, age, vol/liq sanity, one-sided flow)
- **News & narrative heat** — free RSS feeds tagged by narrative; alerts when a
  narrative accelerates vs its 7-day baseline (this is what front-runs rotations)
- **Paper trading** — fake $10k with honest fees + slippage, `core` vs `degen`
  buckets (degen capped at 20%), benchmarked against just-holding-BTC
- **LLM co-pilot** (optional) — daily brief, `/check` devil's-advocate on your
  trade thesis, weekly behavioral review of the journal

## Setup

```
cd crypto-copilot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

1. Telegram: talk to **@BotFather** → `/newbot` → copy the token into `.env`
   as `TELEGRAM_BOT_TOKEN`.
2. (Optional) Anthropic API key into `.env` as `ANTHROPIC_API_KEY` to enable
   the LLM features. Without it everything else still works.
3. Verify data sources without Telegram:

```
.venv\Scripts\python run.py --selftest
```

4. Run the bot:

```
.venv\Scripts\python run.py
```

5. Open your bot in Telegram and send `/start` — that chat becomes the alert
   target. `/help` lists every command.

## Deploy to Railway

Runs 24/7 so listing alerts fire while your PC is off. One service, bot +
dashboard in one container, ~$5/month.

**⚠️ Only one instance may run at a time.** Telegram allows a single poller per
bot token — running locally *and* on Railway gives `Conflict: terminated by
other getUpdates request` and alerts start dropping randomly. Stop the local
process before/after deploying.

```bash
npm i -g @railway/cli && railway login
```

```bash
railway init && railway up
```

Then in the Railway dashboard:

1. **Add a volume** mounted at `/data`. Without it, SQLite is wiped on every
   redeploy — you lose the paper record, which is the whole point.
2. **Variables** — set these (`DATA_DIR` is already baked into the Dockerfile):

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from @BotFather |
   | `ANTHROPIC_API_KEY` | enables /brief, /check, /review |
   | `WEB_USER` / `WEB_PASSWORD` | dashboard login — **required**, the app refuses to serve without it |
   | `TIMEZONE` | `Asia/Colombo` |
   | `BRIEF_HOUR` | `8` |

3. **Generate a domain** (Settings → Networking). That URL is your dashboard.
4. **Region must not be US.** Binance returns `451 Service unavailable from a
   restricted location` to US IPs, which kills prices, funding, movers and
   listing detection while everything else keeps working — a confusing
   half-dead deploy. Use `europe-west4` (Amsterdam). Set it in Settings →
   Scale; `railway.json` deliberately does not pin region or replicas so the
   UI stays authoritative.
5. Keep replicas at **1** (Railway's default) — two containers means two
   Telegram pollers fighting and two writers on one SQLite file.
6. **The volume is region-bound.** Changing region later means recreating it
   and losing the database, so pick the region before accumulating a record.

Health check is `/healthz` (unauthenticated, exposes no data). Everything else
requires the password.

Your local `data/copilot.db` does not transfer. Deploy before you accumulate a
paper record you care about, or copy the file into the volume.

## Run at login (Windows Task Scheduler)

```
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

Creates a "crypto-copilot" task that starts the bot at logon. Remove with
`Unregister-ScheduledTask -TaskName crypto-copilot`.

## Design constraints (on purpose)

- No Binance API key, no wallet, no order execution — the tool surfaces and
  scores; you place every trade manually.
- No presale/ICO tracking (scam-dense, no analyzable data).
- Rug filter shows SKIPped candidates with reasons rather than hiding them.
- Success metric: the paper record vs hold-BTC, per bucket — not vibes.

## Files

- `copilot/data/` — Binance (ccxt public), DexScreener, announcements, RSS, F&G
- `copilot/engine/` — alert rules, narrative heat, rug filter, mover scanner, paper trading
- `copilot/bot/` — Telegram commands + job scheduling
- `copilot/llm/` — Claude Haiku brief / thesis-check / review (optional)
- `data/copilot.db` — SQLite (all state; delete to reset)
- `logs/copilot.log` — rotating log
