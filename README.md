# JP Gold Bot — v2.0 (Sessions 1+2+3)

Production-grade Telegram trading assistant for gold (XAU/USD).
Real strategy logic with Order Blocks, FVGs, zone tracking, DXY confluence,
and proactive alerts.

## What This Bot Does (End-to-End)

1. **Watches gold 2H** during London/NY sessions
2. **Detects fresh 2H BOS** automatically
3. **Marks the Order Block** (last opposing candle before impulse, any color)
4. **Marks the FVG** in the impulse leg if present
5. **Sends zone to you** with Valid/Poor/Edit buttons
6. **You confirm valid** → bot saves zone in memory + permanent Telegram log
7. **Bot watches proximity:** Approaching → Near → Tapped → alerts you each state
8. **Bot watches 15M** for BOS/CHoCH alignment with the zone
9. **Signal fires** when 15M trigger hits inside zone with entry/SL/TP at 3R
10. **DXY confluence** check (must move opposite to your trade)
11. **Auto-invalidation** when zone fails or signal completes
12. **Anti-spam:** each alert state fires only once per zone

## Environment Variables (Render Dashboard)

```
BOT_TOKEN          — Telegram bot token from @BotFather
CHAT_ID            — Your Telegram chat ID (from @userinfobot)
TWELVE_DATA_KEY    — From twelvedata.com (free tier OK)
GROQ_API_KEY       — Optional, for AI chat features
```

## Render Setup

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app`
- **Health check path:** `/health`

## Telegram Setup

Set the webhook URL (one time):
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
- `/health` — bot status
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

## Validate Before Session 4

After a few days of running, watch for:

1. ☐ Bot detects 2H BOS that match what you see on TradingView
2. ☐ Order Blocks marked correctly (last opposing candle before impulse)
3. ☐ FVGs correctly identified when they exist
4. ☐ Proximity alerts fire when expected (Approaching/Near/Tapped)
5. ☐ Signals fire only when 15M trigger aligns
6. ☐ TP is exactly 3R from entry
7. ☐ DXY confluence shows correctly
8. ☐ No spam (each alert fires once per zone)

## Versioning

- v2.0-session1 — Foundation (deployed)
- v2.0-s1s2s3 — **Current: OB + FVG + Zones + DXY + Proactive Alerts**
- v2.0-session4 — Polish + stress test (next session)
