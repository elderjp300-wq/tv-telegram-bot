"""
═══════════════════════════════════════════════════════════════════════
  JP GOLD BOT — v2.0 (Session 1: Foundation — Lightweight Build)
═══════════════════════════════════════════════════════════════════════
  Built for: Johnpaul Uche
  Strategy:  Gold-only, 2H structure + 15M trigger, SMC/ICT
  Stack:     Flask + Telegram + Twelve Data (no pandas/numpy)

  Why no pandas? Render free tier struggles with heavy installs.
  Pandas was only needed for 1H -> 2H resampling, which is a small
  loop in pure Python. This version deploys in 30s and runs leaner.
═══════════════════════════════════════════════════════════════════════
"""

import os
import requests
from datetime import datetime, timezone
from flask import Flask, request

# ─────────────────────────────────────────────────────────────
# CONFIG & ENVIRONMENT
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)

BOT_TOKEN        = os.environ.get("BOT_TOKEN")
CHAT_ID          = os.environ.get("CHAT_ID")
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")  # stubbed in Session 1

# Strategy parameters (centralized for easy tuning)
SWING_LOOKBACK       = 5
ATR_PERIOD           = 14
ATR_SIGNIFICANCE     = 0.5
CONSOLIDATION_RATIO  = 1.5
MIN_RR               = 3.0
RISK_PERCENT         = 0.5

# Session windows (UTC)
LONDON_OPEN, LONDON_CLOSE = 7, 12
NY_OPEN, NY_CLOSE         = 12, 17

LAST_SCAN_TIME = None


# ─────────────────────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────
def send_telegram(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id}, timeout=5)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# SESSION & TIME UTILITIES
# ─────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def is_session_active():
    n = now_utc()
    if n.weekday() >= 5:
        return False
    h = n.hour
    return (LONDON_OPEN <= h < LONDON_CLOSE) or (NY_OPEN <= h < NY_CLOSE)


def get_session_label():
    n = now_utc()
    if n.weekday() >= 5:
        return "Weekend ◯"
    h = n.hour
    if LONDON_OPEN <= h < LONDON_CLOSE:
        return "London ●"
    elif NY_OPEN <= h < NY_CLOSE:
        return "New York ●"
    return "Off-Session ◯"


def get_next_session():
    n = now_utc()
    h = n.hour
    if n.weekday() >= 5:
        return "Markets reopen Monday"
    if h < LONDON_OPEN:
        return f"London opens in {LONDON_OPEN - h}h"
    elif h < LONDON_CLOSE:
        return "London active"
    elif h < NY_CLOSE:
        return "New York active"
    else:
        hours_to_london = (24 - h) + LONDON_OPEN
        return f"London opens in {hours_to_london}h"


def heartbeat_line():
    if LAST_SCAN_TIME:
        last = LAST_SCAN_TIME.strftime("%H:%M UTC")
        return f"☑ Online  ▪︎  Last scan: `{last}`"
    return "☑ Online  ▪︎  Awaiting first scan"


# ─────────────────────────────────────────────────────────────
# DATA LAYER — Twelve Data
# ─────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, outputsize):
    """Fetch raw candles. Returns list of dicts (oldest first) with float OHLC."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON",
    }
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res.get("status") == "error":
            return None
        candles = res.get("values", [])
        if not candles:
            return None
        clean = []
        for c in candles:
            try:
                clean.append({
                    "datetime": c["datetime"],
                    "open":     float(c["open"]),
                    "high":     float(c["high"]),
                    "low":      float(c["low"]),
                    "close":    float(c["close"]),
                })
            except (KeyError, ValueError):
                continue
        clean.reverse()
        return clean
    except Exception:
        return None


def resample_1h_to_2h(candles_1h):
    """
    Pure-Python resampling: groups 1H candles into 2H buckets.
    open=first, high=max, low=min, close=last per 2H bucket.
    """
    if not candles_1h:
        return None

    buckets = {}
    for c in candles_1h:
        dt = datetime.fromisoformat(c["datetime"].replace(" ", "T"))
        bucket_hour = (dt.hour // 2) * 2
        bucket_key = dt.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)

        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "datetime": bucket_key.isoformat(),
                "open":  c["open"],
                "high":  c["high"],
                "low":   c["low"],
                "close": c["close"],
            }
        else:
            b = buckets[bucket_key]
            b["high"]  = max(b["high"], c["high"])
            b["low"]   = min(b["low"],  c["low"])
            b["close"] = c["close"]

    sorted_keys = sorted(buckets.keys())
    return [buckets[k] for k in sorted_keys]


def get_2h_data(bars=30):
    needed_1h = bars * 2 + 4
    candles_1h = fetch_candles("XAU/USD", "1h", needed_1h)
    if not candles_1h or len(candles_1h) < 10:
        return None
    candles_2h = resample_1h_to_2h(candles_1h)
    if not candles_2h or len(candles_2h) < 10:
        return None
    return candles_2h[-bars:]


def get_15m_data(bars=30):
    return fetch_candles("XAU/USD", "15min", bars)


def get_dxy_summary():
    """Stubbed for Session 1. Live DXY wired in Session 3."""
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────
# INDICATOR LAYER (pure Python)
# ─────────────────────────────────────────────────────────────
def compute_atr(candles, period=ATR_PERIOD):
    """Returns list of ATR values aligned with candles. Early values = None."""
    if not candles or len(candles) < period + 1:
        return [None] * len(candles) if candles else []

    tr_list = [None]
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        tr_list.append(tr)

    atr_list = [None] * len(candles)
    for i in range(period, len(candles)):
        window = tr_list[i - period + 1:i + 1]
        if all(x is not None for x in window):
            atr_list[i] = sum(window) / period
    return atr_list


def detect_swings(candles, atr_list, lookback=SWING_LOOKBACK):
    """Returns (swing_highs, swing_lows) as lists of (index, price) tuples."""
    swing_highs = []
    swing_lows = []
    n = len(candles)

    for i in range(lookback, n - lookback):
        window_high = [candles[j]["high"] for j in range(i - lookback, i + lookback + 1)]
        window_low  = [candles[j]["low"]  for j in range(i - lookback, i + lookback + 1)]
        local_avg_close = sum(c["close"] for c in candles[i - lookback:i + lookback + 1]) / (2 * lookback + 1)

        atr_here = atr_list[i] if atr_list[i] is not None else None

        if candles[i]["high"] == max(window_high):
            if atr_here is None or abs(candles[i]["high"] - local_avg_close) >= ATR_SIGNIFICANCE * atr_here:
                swing_highs.append((i, candles[i]["high"]))

        if candles[i]["low"] == min(window_low):
            if atr_here is None or abs(candles[i]["low"] - local_avg_close) >= ATR_SIGNIFICANCE * atr_here:
                swing_lows.append((i, candles[i]["low"]))

    return swing_highs, swing_lows


def is_consolidating(candles, atr_list):
    if len(candles) < 10:
        return False
    recent = candles[-10:]
    range_size = max(c["high"] for c in recent) - min(c["low"] for c in recent)
    recent_atrs = [a for a in atr_list[-10:] if a is not None]
    if not recent_atrs:
        return False
    avg_atr = sum(recent_atrs) / len(recent_atrs)
    if avg_atr == 0:
        return False
    return range_size < (CONSOLIDATION_RATIO * avg_atr)


# ─────────────────────────────────────────────────────────────
# STRUCTURE DETECTION
# ─────────────────────────────────────────────────────────────
def analyze_structure(candles):
    if not candles or len(candles) < 15:
        return None

    atr_list = compute_atr(candles)
    swing_highs, swing_lows = detect_swings(candles, atr_list)
    consolidating = is_consolidating(candles, atr_list)

    current_price = candles[-1]["close"]
    atr_now = atr_list[-1]

    trend = "Ranging"
    bos = "None"
    choch = "None"
    last_high_val = None
    last_low_val = None
    prev_high_val = None
    prev_low_val = None

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        last_high_val = swing_highs[-1][1]
        prev_high_val = swing_highs[-2][1]
        last_low_val  = swing_lows[-1][1]
        prev_low_val  = swing_lows[-2][1]

        if last_high_val > prev_high_val and last_low_val > prev_low_val:
            trend = "Bullish"
        elif last_high_val < prev_high_val and last_low_val < prev_low_val:
            trend = "Bearish"

        if trend == "Bullish" and current_price > prev_high_val:
            bos = f"Bullish BOS @ `{round(prev_high_val, 2)}`"
        elif trend == "Bearish" and current_price < prev_low_val:
            bos = f"Bearish BOS @ `{round(prev_low_val, 2)}`"

        if trend == "Bearish" and current_price > prev_high_val:
            choch = f"Bullish CHoCH @ `{round(prev_high_val, 2)}`"
        elif trend == "Bullish" and current_price < prev_low_val:
            choch = f"Bearish CHoCH @ `{round(prev_low_val, 2)}`"

    recent_window = candles[-20:] if len(candles) >= 20 else candles
    return {
        "trend":           trend,
        "current_price":   round(current_price, 2),
        "recent_high":     round(max(c["high"] for c in recent_window), 2),
        "recent_low":      round(min(c["low"]  for c in recent_window), 2),
        "bos":             bos,
        "choch":           choch,
        "last_swing_high": round(last_high_val, 2) if last_high_val else None,
        "last_swing_low":  round(last_low_val, 2)  if last_low_val  else None,
        "consolidating":   consolidating,
        "atr":             round(atr_now, 2) if atr_now else None,
    }


# ─────────────────────────────────────────────────────────────
# DISPLAY MESSAGES
# ─────────────────────────────────────────────────────────────
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━"


def back_button():
    return {"inline_keyboard": [[{"text": "▪︎ Back to Dashboard", "callback_data": "dashboard"}]]}


def main_menu():
    return {
        "inline_keyboard": [
            [{"text": "🪙  Gold — Deep Analysis", "callback_data": "analyze_gold"}],
            [{"text": "🔔  Force Scan",           "callback_data": "force_scan"}],
            [
                {"text": "▫️ Trade Log",     "callback_data": "trade_log"},
                {"text": "▫️ Session",       "callback_data": "session_info"},
            ],
            [
                {"text": "▫️ Entry Rules",   "callback_data": "rules"},
                {"text": "▫️ A+ Checklist",  "callback_data": "checklist"},
            ],
        ]
    }


def dashboard_message():
    n = now_utc().strftime("%H:%M UTC")
    status = "● MARKET ACTIVE" if is_session_active() else "◯ MARKET CLOSED"
    return f"""
*JP GOLD BOT*  ▪︎  v2.0
{DIVIDER}
{status}
{get_session_label()}  ▪︎  `{n}`
{get_next_session()}
{DIVIDER}
*Asset:*    🪙 XAU/USD only
*Method:*   2H structure → 15M trigger
*System:*   SMC ▪︎ Min 3R ▪︎ 0.5% risk
{DIVIDER}
{heartbeat_line()}
"""


def rules_message():
    return f"""
⚔️  *JP GOLD STRATEGY — ENTRY RULES*
{DIVIDER}

*1.  HTF READ (2H)*
   ▪︎  Identify trend (HH/HL bullish or LH/LL bearish)
   ▪︎  Wait for Break of Structure (BOS)

*2.  ZONE MARKING*
   ▪︎  Locate the imbalance/FVG that caused the BOS
   ▪︎  Mark Order Block (last candle before impulse, any color)
   ▪︎  Confirm zone validity manually (Valid / Poor)

*3.  ENTRY TRIGGER (15M)*
   ▪︎  Wait for price to approach or tap marked zone
   ▪︎  Look for 15M BOS or CHoCH inside or near zone
   ▪︎  Entry only on confirmation

*4.  CONFLUENCE*
   ▪︎  DXY moving opposite to trade direction
   ▪︎  Min 3R risk-reward
   ▪︎  Active session: London or New York

*5.  RISK*
   ▪︎  0.5% per trade
   ▪︎  No Friday evening holds
   ▪︎  No revenge trades after stop-out

{DIVIDER}
⚠ Miss any rule → NO TRADE
"""


def checklist_message():
    return f"""
☑  *JP GOLD A+ CHECKLIST*
{DIVIDER}
*Pre-trade — must pass:*

▫️  2H trend clear (Bullish or Bearish)
▫️  Fresh 2H BOS confirmed
▫️  Zone marked and validated by me
▫️  Price approaching, near, or inside zone
▫️  15M BOS or CHoCH at the zone
▫️  DXY moving opposite to trade
▫️  Active session (London / NY)
▫️  Min 3R achievable to logical TP

{DIVIDER}
*8/8*  → SIGNAL FIRES
*6–7/8*  → WATCHLIST
*<6*  → NO TRADE
⚠ If any of the first 5 missing → SKIP
"""


def session_status_message():
    n = now_utc().strftime("%H:%M UTC")
    active = is_session_active()
    return f"""
*SESSION STATUS*
{DIVIDER}
Current:    {get_session_label()}
Time:       `{n}`
Status:     {"● Active — trade your system" if active else "◯ Closed — wait for next session"}
Next:       {get_next_session()}
{DIVIDER}
_London:_     `07:00 – 12:00 UTC`
_New York:_   `12:00 – 17:00 UTC`
"""


def trade_log_message():
    return f"""
📓  *TRADE LOG — Search Reference*
{DIVIDER}
All logs are saved as messages in this chat.
Search by hashtag in Telegram:

▫️  *All logs:*       `#log`
▫️  *Gold only:*      `#gold`
▫️  *Active zones:*   `#zone #active`
▫️  *Closed zones:*   `#zone #closed`
▫️  *London only:*    `#london`
▫️  *NY only:*        `#newyork`

{DIVIDER}
_Every signal and zone is auto-logged._
"""


def format_structure_card(structure):
    trend_icon = {"Bullish": "▲", "Bearish": "▼", "Ranging": "◆"}.get(structure["trend"], "◆")
    consolidation_note = ""
    if structure["consolidating"]:
        consolidation_note = "\n⚠  *Consolidating range* — wait for breakout"

    swing_h = structure["last_swing_high"]
    swing_l = structure["last_swing_low"]
    swing_str = ""
    if swing_h and swing_l:
        swing_str = f"\nSwings:     ▲ `{swing_h}`  ▼ `{swing_l}`"

    atr_str = f"\nATR(14):    `{structure['atr']}`" if structure["atr"] else ""

    return f"""
🪙  *GOLD — 2H Structure*
{DIVIDER}
Price:      `{structure['current_price']}`
Trend:      {trend_icon}  {structure['trend']}
BOS:        {structure['bos']}
CHoCH:      {structure['choch']}{swing_str}{atr_str}
Session:    {get_session_label()}{consolidation_note}
{DIVIDER}
"""


# ─────────────────────────────────────────────────────────────
# CORE ANALYSIS FLOW
# ─────────────────────────────────────────────────────────────
def run_gold_analysis():
    global LAST_SCAN_TIME

    if not is_session_active():
        return False, {
            "headline": "◯  *Market Closed*",
            "body": f"Gold analysis only runs during London or NY sessions.\n\n_{get_next_session()}_",
        }

    candles_2h = get_2h_data(bars=30)
    if not candles_2h:
        return False, {
            "headline": "⚠  *Data Unavailable*",
            "body": "Could not fetch gold 2H data from provider.\nTry Force Scan again in a moment.",
        }

    structure = analyze_structure(candles_2h)
    if structure is None:
        return False, {
            "headline": "⚠  *Structure Analysis Failed*",
            "body": "Insufficient swing data on 2H. Try again later.",
        }

    LAST_SCAN_TIME = now_utc()
    card = format_structure_card(structure)
    return True, {"headline": None, "body": card, "structure": structure}


def auto_market_scan():
    global LAST_SCAN_TIME
    if not is_session_active():
        return
    candles_2h = get_2h_data(bars=30)
    if not candles_2h:
        return
    structure = analyze_structure(candles_2h)
    if structure is None:
        return
    LAST_SCAN_TIME = now_utc()


# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────
@app.route("/")
def root():
    auto_market_scan()
    return "ok", 200


@app.route("/health")
def health():
    return {
        "status": "online",
        "session_active": is_session_active(),
        "session": get_session_label(),
        "last_scan": LAST_SCAN_TIME.isoformat() if LAST_SCAN_TIME else None,
        "version": "2.0-session1-lite",
    }, 200


@app.route("/startup")
def startup():
    if CHAT_ID:
        send_telegram(CHAT_ID, dashboard_message(), main_menu())
    return "ok", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return "ok", 200

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        action = cb["data"]
        answer_callback(cb["id"])

        if action == "dashboard":
            send_telegram(chat_id, dashboard_message(), main_menu())
        elif action == "session_info":
            send_telegram(chat_id, session_status_message(), back_button())
        elif action == "rules":
            send_telegram(chat_id, rules_message(), back_button())
        elif action == "checklist":
            send_telegram(chat_id, checklist_message(), back_button())
        elif action == "trade_log":
            send_telegram(chat_id, trade_log_message(), back_button())
        elif action in ("analyze_gold", "force_scan"):
            label = "*Analyzing gold...*" if action == "analyze_gold" else "*Force scanning gold...*"
            send_telegram(chat_id, label)
            ok, result = run_gold_analysis()
            if ok:
                send_telegram(chat_id, result["body"], back_button())
            else:
                msg = f"{result['headline']}\n{DIVIDER}\n{result['body']}"
                send_telegram(chat_id, msg, back_button())

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")

        if text in ["/start", "/menu", "/dashboard"]:
            send_telegram(chat_id, dashboard_message(), main_menu())
        elif text == "/scan":
            send_telegram(chat_id, "*Force scanning gold...*")
            ok, result = run_gold_analysis()
            if ok:
                send_telegram(chat_id, result["body"], back_button())
            else:
                send_telegram(chat_id, f"{result['headline']}\n{DIVIDER}\n{result['body']}", back_button())
        elif text == "/health":
            send_telegram(
                chat_id,
                f"☑ *Bot Health*\n{DIVIDER}\nVersion: `2.0-session1-lite`\nSession: {get_session_label()}\n{heartbeat_line()}",
                back_button(),
            )
        elif text == "/rules":
            send_telegram(chat_id, rules_message(), back_button())
        elif text == "/checklist":
            send_telegram(chat_id, checklist_message(), back_button())
        else:
            send_telegram(chat_id, "_Tap a button on the dashboard, or use_ `/menu`", back_button())

    return "ok", 200


# ─────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
