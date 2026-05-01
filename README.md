# JP Gold Bot — v2.0 (Final / Sessions 1+2+3+4)

Production-grade Telegram trading assistant for gold (XAU/USD).
Real strategy logic with Order Blocks, FVGs, zone tracking, DXY confluence,
proactive alerts, error handling, and observability.

## What This Bot Does (End-to-End)

1. **Watches gold 2H** during London/NY sessions (no weekends, no off-hours)
2. **Detects fresh 2H BOS** automatically
3. **Marks the Order Block** (last opposing candle before impulse, any color)
4. **Marks the FVG** in the impulse leg if present
5. **Validates zone freshness** — skips zones already tapped or with new structure
6. **Sends zone to you** with Valid/Poor/Edit buttons
7. **You confirm valid** → bot saves zone in memory + permanent Telegram log
8. **Bot watches proximity:** Approaching → Near → Tapped → alerts you each state
9. **Bot watches 15M** for BOS/CHoCH alignment with the zone
10. **Signal fires** when 15M trigger hits inside zone with entry/SL/TP at exactly 3R
11. **DXY confluence** check (must move opposite to your trade)
12. **Auto-invalidation** when zone fails or signal completes
13. **Friday wind-down** at 15:00 UTC: no new signals, clears active zones
14. **Error tracking** with `/status` for Monday troubleshooting
15. **Anti-spam:** each alert state fires only once per zone

## Environment Variables (Render Dashboard)

```
BOT_TOKEN          — Telegram bot token from @BotFather
CHAT_ID            — Your Telegram chat ID (from @userinfobot)
TWELVE_DATA_KEY    — From twelvedata.com (free tier OK)
GROQ_API_KEY       — Optional, for AI features (not used in current build)
```

## Render Setup

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app`
- **Health check path:** `/health`

## Telegram Setup

Set the webhook URL (one time, only if URL changes):
```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<RENDER_URL>/webhook
```

## UptimeRobot

Monitor URL: `https://<RENDER_URL>/`
Interval: 5 minutes
This both keeps Render warm AND triggers `auto_market_scan()` proactively.

## Manual Commands

- `/menu` or `/start` — show dashboard
- `/scan` — force gold analysis
- `/zones` — list active zones
- `/status` — **detailed bot diagnostics** (use this for troubleshooting)
- `/config` — show current strategy parameters
- `/health` — quick health check
- `/rules` — entry rules
- `/checklist` — A+ checklist

## How To Use The Zone Workflow

When the bot detects a fresh 2H BOS, it sends you a zone proposal:

```
🔔 NEW ZONE DETECTED — GOLD
Direction: ▲ BULLISH
OB:        4598 – 4612
FVG:       4615 – 4620
Total zone: 4598 – 4620
2H BOS at:  4598

[✓ Valid] [✗ Poor]
[✏️ Edit]
[Back to Dashboard]
```

- **Valid:** bot saves zone, watches it, alerts you on approach/trigger
- **Poor:** zone discarded
- **Edit:** bot prompts for corrected zone — reply with `high, low` (e.g., `4605, 4598`)

## Active Zones Behavior

- Up to **2 active zones** at a time (oldest replaced if more come)
- Zones auto-invalidate when price closes beyond them in wrong direction
- Zone is removed when signal fires (one-shot use)
- All zones cleared automatically Friday 15:00 UTC

## Zone Freshness Rule

When a 2H BOS forms, the bot checks before proposing the zone:
- **Has price already tapped the zone?** If yes, don't propose (smart money already filled).
- **Has price already closed beyond it (broken structure)?** If yes, don't propose (setup is dead).
- **Otherwise**, propose it for your validation.

This means even if Render restarts and bot wakes up to find a "stale" BOS,
it won't propose dead setups.

## Friday Wind-Down

At 15:00 UTC on Fridays:
- Bot stops firing new signals
- All active zones are cleared
- You get a wind-down notification
- Bot resumes Monday at London open (07:00 UTC)

## Monday Troubleshooting Guide

If something feels off Monday morning:

1. **Tap `/status`** — shows last scan times, last DXY check, last error, active zones
2. **Tap `/health`** — quick alive check
3. **Tap `/config`** — verify parameters haven't drifted
4. **Visit `<RENDER_URL>/health`** in browser — JSON status from Render itself

Common issues and how `/status` will reveal them:
- **No scans happening:** "Last scan" will be old, "Last error" will show why
- **DXY broken:** "DXY" line will say UNAVAILABLE
- **Twelve Data quota hit:** Errors will mention "API error" or rate limit
- **Render cold start:** First request after idle = 30+ sec delay (normal on free tier)

## Versioning

- v2.0-session1 — Foundation
- v2.0-s1s2s3 — Real strategy logic
- **v2.0-s4-final — Current build (production)**

## Architecture Summary

```
┌─ Twelve Data API ──────────┐
│   1H gold → resampled 2H   │
│   15M gold                 │
│   1H DXY                   │
└────────────┬───────────────┘
             ↓
┌─ Pure-Python indicators ───┐
│   ATR, swings, FVG, OB     │
└────────────┬───────────────┘
             ↓
┌─ Strategy logic ───────────┐
│   2H BOS → OB+FVG zone     │
│   User validates           │
│   Proximity tracking       │
│   15M trigger              │
│   DXY confluence           │
│   Friday wind-down         │
└────────────┬───────────────┘
             ↓
┌─ Telegram interface ───────┐
│   Dashboard menu           │
│   Inline buttons           │
│   Hashtag-based logging    │
│   Status diagnostics       │
└────────────────────────────┘
```

No pandas, no numpy, no SQLite. Just Flask, requests, and pure Python.
Deploys on Render free tier in ~30 seconds.
