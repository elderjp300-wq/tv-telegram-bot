# JP Gold Bot — v2.0 (Session 1: Foundation)

Production-grade Telegram trading assistant for gold (XAU/USD).
Built around a 2H structure + 15M trigger SMC strategy.

## What's In This Session

This is **Session 1 of 4** in the bot rebuild.

**Included:**
- Gold-only analysis (no other pairs)
- Hard session gating (London 7-12 UTC, NY 12-17 UTC, weekends off)
- 1H → 2H pandas resampling (Twelve Data doesn't natively serve 2H)
- ATR(14) calculation
- Consolidation detector (skips choppy markets)
- Improved swing detection (lookback=5 + ATR significance filter)
- Clean back-button UX (no menu-stuffing under every message)
- Heartbeat indicator on the dashboard
- DXY hook stubbed (live wiring in Session 3)

**NOT yet included (coming Sessions 2-4):**
- Order Block detection
- FVG/Imbalance detection
- Zone storage + valid/poor workflow
- Live DXY confluence
- Proactive alerts (zone approach, BOS firing, session opens)

## Environment Variables (Render Dashboard)

```
BOT_TOKEN          — Telegram bot token from @BotFather
CHAT_ID            — Your Telegram chat ID (from @userinfobot)
TWELVE_DATA_KEY    — From twelvedata.com (free tier OK)
GROQ_API_KEY       — From console.groq.com (stubbed in S1, needed S3)
```

## Render Setup

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app`
- **Health check path:** `/health`

## Telegram Setup

Set the webhook URL (run once):
```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<RENDER_URL>/webhook
```

## UptimeRobot

Monitor URL: `https://<RENDER_URL>/`
Interval: 5 minutes
This both keeps Render warm AND triggers `auto_market_scan()`.

## Manual Testing

- `/menu` — show dashboard
- `/scan` — force gold analysis
- `/health` — bot status
- `/rules` — entry rules
- `/checklist` — A+ checklist

## What To Validate Before Session 2

1. ☐ Dashboard loads cleanly with single back-button UX
2. ☐ Force Scan returns 2H structure card with current gold price
3. ☐ Trend / BOS / CHoCH show correct values vs your TradingView read
4. ☐ Heartbeat updates after each scan
5. ☐ Off-session attempts return "Market Closed" (test on weekend or after 17:00 UTC)
6. ☐ Consolidation detector triggers on chop (visually verify on chart)
7. ☐ Swings look clean — no over-detection of micro-wiggles

## Versioning

- v2.0-session1 — Foundation (this file)
- v2.0-session2 — OB + FVG + Zone workflow (next)
- v2.0-session3 — DXY + Proactive alerts
- v2.0-session4 — Polish + stress test
