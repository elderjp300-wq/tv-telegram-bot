# JP Gold Bot — v3.0

**15M Textbook SMC strategy with composite grading and full trade lifecycle management.**

Single-instrument (XAUUSD) Telegram trading assistant. Detects Break of Structure on the 15M timeframe, validates against 2H trend context, grades setups A+ through C, and walks each trade through its full lifecycle from limit-fill to TP/SL with notifications at every stage.

This is an **alerting bot, not an execution bot.** It does not place orders. It tells you when to act.

---

## What changed from v2

v2 tracked zones across multiple states (born → approaching → near → tapped → validated → triggered). v3 is event-driven: BOS happens, signal fires immediately, trade is tracked through completion.

- ❌ Removed: multi-stage proximity tracking (the source of v2's "tapped without proximity alert" bug)
- ❌ Removed: 2H zone state management (2H is now context-only)
- ❌ Removed: DXY confluence (Twelve Data USDIDX issue made irrelevant)
- ❌ Removed: multi-pair scanning (XAUUSD only per trading rules)
- ✅ Added: composite grading (A+/A/B/C) with explicit factor breakdown
- ✅ Added: full trade lifecycle (PENDING → ACTIVE → TP1 → TP2 / SL / EXPIRED)
- ✅ Added: add-on signal detection with explicit "move parent SL to BE" reminders
- ✅ Added: Taken/Skipped buttons replace Valid/Poor/Edit (decisions now tied to actual trades)

---

## Strategy in plain English

1. **2H trend context.** Fetch 80 bars of 2H candles, identify swing structure. Result is one of: `bullish`, `bearish`, or `range`. Used only for grading; no zones tracked.

2. **15M setup detection.** Every 2 minutes during London + NY sessions, fetch 120 bars of 15M, detect if the most recent closed candle broke a swing high (bullish BOS) or swing low (bearish BOS).

3. **OB & FVG identification.** Walk back from the BOS candle to find the last opposing-color candle inside the impulse leg (the Order Block). Scan the impulse for a 3-bar Fair Value Gap.

4. **Levels.** Entry = OB open. SL = OB low (bullish) or OB high (bearish), plus $0.50 buffer. TP1 = 3R fixed. TP2 = nearest 2H swing high (bullish) or low (bearish) beyond TP1.

5. **Grading.** Six-factor composite, max 6 points:
   - +2 if 15M direction aligns with 2H trend
   - +1 if FVG present in impulse
   - +1 if London session, +1 if NY session (one or the other applies)
   - +1 clean OB (no prior taps)
   - +1 room to nearest opposing 2H swing greater than 1.5R
   - **A+** = 5–6 pts · **A** = 4 pts · **B** = 3 pts · **C** = 0–2 pts

6. **Risk per grade.**
   - A+/A: 0.5%
   - B/C: 0.25%

All grades are sent to Telegram. User decides which to take.

---

## Trade lifecycle

```
SIGNAL FIRED
    │
    ▼
[PENDING] ────── 8 bars no tap ──→ [EXPIRED]
    │
    │ price reaches OB
    ▼
[ACTIVE] ──── price reverses to SL ──→ [SL_HIT]
    │
    │ price reaches TP1 (3R)
    ▼
[TP1_HIT] ────── (SL moves to BE) ────→ continues running
    │
    │ price reaches TP2 (2H swing)
    ▼
[TP2_HIT]  (full close)
```

Every transition sends a Telegram message:
- **SIGNAL** → entry + SL + TP1 + TP2 + grade + factor breakdown
- **TAPPED** → trade is now live
- **TP1 HIT** → close partial, move runner SL to BE
- **TP2 HIT** → full close
- **SL HIT** → loss recorded
- **EXPIRED** → never tapped, signal canceled

---

## Add-on signals

While any trade is in PENDING or ACTIVE state, the bot keeps scanning. If a same-direction setup forms, it fires an **ADD-ON** alert (different from a fresh SIGNAL):

- Header explicitly says `ADD-ON` not `SIGNAL`
- Message includes: "if taken, move parent Trade [ID] SL to BE at [entry]"
- Same grading, same risk-per-grade rules
- Tracked as its own trade through its own lifecycle

Add-ons fire regardless of parent trade's P&L state — the warning text is the safeguard.

---

## Sessions and Friday wind-down

- Signal generation: **07:00–22:00 UTC** (London + NY)
- Lifecycle tracking: 24/5 (existing trades watched even off-session)
- Friday wind-down: **17:00 UTC** — clears all PENDING signals, warns if ACTIVE trades are still running
- No weekend signals

---

## Telegram commands

| Command | Purpose |
|---|---|
| `/start`, `/help` | Bot description and command list |
| `/status` | Live diagnostics: paused state, last scan, current 2H trend, active book, pending/active counts |
| `/config` | All strategy parameters in one message |
| `/pause` | Stop generating new signals (lifecycle tracking continues) |
| `/resume` | Resume signal generation |
| `/clear_trades` | Drop all active trades (use only when bot is in bad state) |
| `/test_functions` | End-to-end smoke test — verifies Twelve Data, Telegram creds, state file write |

Inline buttons on every signal/add-on: **Taken** / **Skipped**. Decisions saved to state for future analysis.

---

## Architecture

- **Flask** — HTTP surface for Render health checks and HTTP-based `/test_functions`
- **APScheduler** — runs scan every 120 seconds, Friday wind-down hourly check
- **python-telegram-bot 20.x** — async Application for command handlers, runs in background thread
- **requests** — synchronous Twelve Data fetches and direct Telegram sendMessage (avoids cross-thread async)
- **bot_state.json** — persistent state for active and completed trades

---

## Environment variables

Required:

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Bot API token from BotFather |
| `TELEGRAM_CHAT_ID` | Where alerts are sent |
| `TWELVE_DATA_KEY` | OHLC data source |

Optional (with defaults):

| Variable | Default | Purpose |
|---|---|---|
| `ACCOUNT_SIZE` | 5000 | Used to compute dollar risk in alerts |
| `PORT` | 10000 | Render sets this automatically |

---

## Deployment (Render)

1. Push `app.py` and `requirements.txt` to the `main` branch
2. Render auto-detects the push and redeploys
3. Build logs should show `Successfully installed apscheduler-...` during pip install
4. Startup log: `JP Gold Bot v3.0 online`
5. First Telegram ping confirms end-to-end health

After deploy, immediately run:
- Telegram: `/test_functions` — all six checks should be ✅
- Telegram: `/status` — should show "Last 2H trend: bullish/bearish/range" and zero trades
- HTTP: `GET /test_functions` — JSON should show all true

---

## Known limitations

- **Render free tier cold starts.** If the service spins down due to inactivity, the scheduler restarts and any in-memory state not yet saved is lost. The `bot_state.json` persistence covers most cases, but the 2-minute scan cycle may have one cold-start gap. Upgrading Render to paid tier eliminates this.
- **Twelve Data rate limits.** Free tier is 800 requests/day. At 2-minute scan with 2 fetches per scan, that's ~1440/day. Will need either fewer scans, longer interval, or paid Twelve Data plan after testing phase.
- **Trade IDs based on BOS candle time.** Means re-scanning the same candle within its 15M window won't duplicate signals. Risk: if Twelve Data returns slightly different timestamps for the same candle across calls, the dedup could fail. Watch for duplicate signals in testing.

---

## Versioning

- **v2.0-original** — multi-pair zone-based system (archived as GitHub Release)
- **v3.0** — current — 15M textbook SMC, XAUUSD only

To roll back to v2: download source from Releases → `v2.0-original` → replace files → commit.

---

## Roadmap

Things deliberately left out of v3 to keep the prototype small. Add only after live data justifies them:

- **Outcome learning.** Track which grades the user actually takes (via Taken/Skipped buttons), report monthly: "you took 12 A+ signals, 8 wins. 4 B signals, 0 wins." Lets the user prune their own filter.
- **News blackout.** Block signals N minutes around major scheduled releases. Requires economic calendar feed.
- **R-multiple tracker.** Cumulative R across all completed trades, broken out by grade.
- **Slack/Discord mirror.** Same alerts to a secondary channel.
- **Backtest harness.** Replay the strategy against historical Twelve Data to validate before going live with parameter changes.

---

## License

Personal use. Not for redistribution.

---

*Built collaboratively. JP Capital — Quant Dominance.*
