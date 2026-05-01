"""
═══════════════════════════════════════════════════════════════════════
  JP GOLD BOT — v2.0 (Session 1: Foundation)
═══════════════════════════════════════════════════════════════════════
  Built for: Johnpaul Uche
  Strategy:  Gold-only, 2H structure + 15M trigger, SMC/ICT
  Stack:     Flask + Telegram + Twelve Data + Groq (stubbed) + Render
  
  Session 1 Scope:
    - Strip all non-gold pairs and Fibonacci logic
    - Rewire menu: Gold | Force Scan | Trade Log | Rules | Checklist | Session
    - Add 1H -> 2H resampling (pandas)
    - Add ATR + consolidation detector
    - Improve swing detection (lookback=5, ATR significance filter)
    - Hard session gating (no exceptions)
    - Heartbeat indicator
    - Clean back-button UX (no menu-stuffing)
    - Stub DXY hook for Session 3
═══════════════════════════════════════════════════════════════════════
"""

import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
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
SWING_LOOKBACK       = 5         # bars on each side for swing detection
ATR_PERIOD           = 14        # ATR window
ATR_SIGNIFICANCE     = 0.5       # swing must clear 0.5 * ATR to count
CONSOLIDATION_RATIO  = 1.5       # range must exceed 1.5 * ATR to trade
MIN_RR               = 3.0       # minimum risk-reward
RISK_PERCENT         = 0.5       # per trade
APPROACH_ATR         = 1.5       # within 1.5 * ATR = approaching zone
NEAR_ATR             = 0.5       # within 0.5 * ATR = near zone

# Session windows (UTC)
LONDON_OPEN, LONDON_CLOSE = 7, 12
NY_OPEN, NY_CLOSE         = 12, 17

# Last scan tracker (for heartbeat)
LAST_SCAN_TIME = None


# ─────────────────────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────
def send_telegram(chat_id, text, reply_markup=None):
    """Send a Telegram message, optionally with inline buttons."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass  # silent fail; logging can be added later


def answer_callback(callback_query_id):
    """Acknowledge an inline button press to stop the loading spinner."""
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
    """Return True only during London or NY sessions (weekdays)."""
    n = now_utc()
    if n.weekday() >= 5:  # 5=Sat, 6=Sun -> markets closed
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
        # after NY close
        hours_to_london = (24 - h) + LONDON_OPEN
        return f"London opens in {hours_to_london}h"


def heartbeat_line():
    """Tiny status line shown on the dashboard."""
    if LAST_SCAN_TIME:
        last = LAST_SCAN_TIME.strftime("%H:%M UTC")
        return f"☑ Online  ▪︎  Last scan: `{last}`"
    return "☑ Online  ▪︎  Awaiting first scan"


# ─────────────────────────────────────────────────────────────
# DATA LAYER — Twelve Data
# ─────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, outputsize):
    """Fetch raw candles from Twelve Data. Returns list of dicts or None."""
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
        candles.reverse()  # oldest first
        return candles
    except Exception:
        return None


def candles_to_df(candles):
    """Convert raw Twelve Data candles to a clean OHLC DataFrame."""
    if not candles:
        return None
    df = pd.DataFrame(candles)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def get_2h_data(bars=30):
    """
    Pull 1H gold candles, resample to 2H locally.
    Twelve Data doesn't natively support 2H. We fetch bars*2 hours of 1H,
    then resample using pandas. Returns DataFrame with 2H OHLC.
    """
    needed_1h = bars * 2 + 4  # buffer
    candles_1h = fetch_candles("XAU/USD", "1h", needed_1h)
    df_1h = candles_to_df(candles_1h)
    if df_1h is None or len(df_1h) < 10:
        return None

    ohlc_rules = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    df_2h = df_1h.resample("2h").agg(ohlc_rules).dropna()
    return df_2h.tail(bars).copy()


def get_15m_data(bars=30):
    """Pull 15M gold candles directly."""
    candles = fetch_candles("XAU/USD", "15min", bars)
    df = candles_to_df(candles)
    if df is None or len(df) < 10:
        return None
    return df


def get_dxy_summary():
    """
    STUB for Session 3.
    Returns one of: 'BULLISH', 'BEARISH', 'NEUTRAL', 'UNAVAILABLE'.
    For Session 1, returns NEUTRAL as a placeholder.
    """
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────
# INDICATOR LAYER
# ─────────────────────────────────────────────────────────────
def add_atr(df, period=ATR_PERIOD):
    """Add ATR column to a DataFrame."""
    df = df.copy()
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["true_range"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr"] = df["true_range"].rolling(window=period).mean()
    return df


def detect_swings(df, lookback=SWING_LOOKBACK):
    """
    Detect swings using centered rolling window AND ATR significance filter.
    A swing is only valid if it clears 0.5 * ATR from the surrounding price.
    Returns the DataFrame with swing_high and swing_low boolean columns.
    """
    df = df.copy()
    window = 2 * lookback + 1

    df["rolling_high"] = df["high"].rolling(window=window, center=True).max()
    df["rolling_low"]  = df["low"].rolling(window=window, center=True).min()

    raw_swing_high = df["high"] == df["rolling_high"]
    raw_swing_low  = df["low"]  == df["rolling_low"]

    # ATR significance filter — swing must clear meaningful distance
    # from local average to count
    if "atr" in df.columns:
        # local context = rolling mean of close over the window
        local_mean = df["close"].rolling(window=window, center=True).mean()
        threshold = df["atr"] * ATR_SIGNIFICANCE

        df["swing_high"] = raw_swing_high & ((df["high"] - local_mean).abs() >= threshold)
        df["swing_low"]  = raw_swing_low  & ((df["low"]  - local_mean).abs() >= threshold)
    else:
        df["swing_high"] = raw_swing_high
        df["swing_low"]  = raw_swing_low

    return df


def is_consolidating(df):
    """
    Returns True if last 10 bars are in tight range (<1.5 * ATR).
    Used to skip choppy markets.
    """
    if len(df) < 10 or "atr" not in df.columns:
        return False
    recent = df.tail(10)
    range_size = recent["high"].max() - recent["low"].min()
    avg_atr = recent["atr"].mean()
    if pd.isna(avg_atr) or avg_atr == 0:
        return False
    return range_size < (CONSOLIDATION_RATIO * avg_atr)


# ─────────────────────────────────────────────────────────────
# STRUCTURE DETECTION
# ─────────────────────────────────────────────────────────────
def analyze_structure(df):
    """
    Full structural analysis on a 2H DataFrame.
    Returns a dict with: trend, current_price, recent_high, recent_low,
    bos, choch, last_swing_high, last_swing_low, consolidating, atr.
    """
    if df is None or len(df) < 15:
        return None

    df = add_atr(df)
    df = detect_swings(df)

    consolidating = is_consolidating(df)

    swings_h = df[df["swing_high"]]["high"].dropna()
    swings_l = df[df["swing_low"]]["low"].dropna()

    current_price = float(df["close"].iloc[-1])
    atr_now = float(df["atr"].iloc[-1]) if not pd.isna(df["atr"].iloc[-1]) else None

    # Default values
    trend = "Ranging"
    bos = "None"
    choch = "None"
    last_high_val = None
    last_low_val = None
    prev_high_val = None
    prev_low_val = None

    if len(swings_h) >= 2 and len(swings_l) >= 2:
        last_high_val = float(swings_h.iloc[-1])
        prev_high_val = float(swings_h.iloc[-2])
        last_low_val  = float(swings_l.iloc[-1])
        prev_low_val  = float(swings_l.iloc[-2])

        if last_high_val > prev_high_val and last_low_val > prev_low_val:
            trend = "Bullish"
        elif last_high_val < prev_high_val and last_low_val < prev_low_val:
            trend = "Bearish"

        # BOS: price has broken the prior structural reference
        if trend == "Bullish" and current_price > prev_high_val:
            bos = f"Bullish BOS @ `{round(prev_high_val, 2)}`"
        elif trend == "Bearish" and current_price < prev_low_val:
            bos = f"Bearish BOS @ `{round(prev_low_val, 2)}`"

        # CHoCH: counter-trend break = first reversal sign
        if trend == "Bearish" and current_price > prev_high_val:
            choch = f"Bullish CHoCH @ `{round(prev_high_val, 2)}`"
        elif trend == "Bullish" and current_price < prev_low_val:
            choch = f"Bearish CHoCH @ `{round(prev_low_val, 2)}`"

    return {
        "trend":           trend,
        "current_price":   round(current_price, 2),
        "recent_high":     round(float(df["high"].tail(20).max()), 2),
        "recent_low":      round(float(df["low"].tail(20).min()), 2),
        "bos":             bos,
        "choch":           choch,
        "last_swing_high": round(last_high_val, 2) if last_high_val else None,
        "last_swing_low":  round(last_low_val, 2)  if last_low_val  else None,
        "consolidating":   consolidating,
        "atr":             round(atr_now, 2) if atr_now else None,
    }


# ─────────────────────────────────────────────────────────────
# DISPLAY MESSAGES (clean, professional formatting)
# ─────────────────────────────────────────────────────────────
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━"


def back_button():
    """Single back-to-dashboard button. No menu stuffing."""
    return {"inline_keyboard": [[{"text": "▪︎ Back to Dashboard", "callback_data": "dashboard"}]]}


def main_menu():
    """The full menu — only attached to the dashboard message."""
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
    """Format the 2H structure analysis as a clean card."""
    trend_icon = {"Bullish": "▲", "Bearish": "▼", "Ranging": "◆"}.get(structure["trend"], "◆")

    consolidation_note = ""
    if structure["consolidating"]:
        consolidation_note = f"\n⚠  *Consolidating range* — wait for breakout"

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
    """
    Main analysis pipeline. Returns (success, message_dict).
    Used both by manual buttons and auto-scan.
    """
    global LAST_SCAN_TIME

    # Hard session gate — no analysis when market is closed
    if not is_session_active():
        return False, {
            "headline": "◯  *Market Closed*",
            "body": f"Gold analysis only runs during London or NY sessions.\n\n_{get_next_session()}_",
        }

    df_2h = get_2h_data(bars=30)
    if df_2h is None:
        return False, {
            "headline": "⚠  *Data Unavailable*",
            "body": "Could not fetch gold 2H data from provider.\nTry Force Scan again in a moment.",
        }

    structure = analyze_structure(df_2h)
    if structure is None:
        return False, {
            "headline": "⚠  *Structure Analysis Failed*",
            "body": "Insufficient swing data on 2H. Try again later.",
        }

    LAST_SCAN_TIME = now_utc()
    card = format_structure_card(structure)

    # In Session 1 we just return the structure card.
    # Session 2 will add OB/FVG marking and zone workflow on top.
    return True, {"headline": None, "body": card, "structure": structure}


# ─────────────────────────────────────────────────────────────
# AUTO-SCAN (triggered by UptimeRobot pings)
# ─────────────────────────────────────────────────────────────
def auto_market_scan():
    """
    Lightweight scan that runs every UptimeRobot ping (~5 min).
    For Session 1: just refreshes scan time and silently checks structure.
    Session 2 will add proactive zone-state alerts.
    """
    global LAST_SCAN_TIME
    if not is_session_active():
        return  # silent during off-hours

    df_2h = get_2h_data(bars=30)
    if df_2h is None:
        return
    structure = analyze_structure(df_2h)
    if structure is None:
        return
    LAST_SCAN_TIME = now_utc()
    # Future: trigger proactive alerts here (Session 2/3)


# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────
@app.route("/")
def root():
    """UptimeRobot pings here every 5 min."""
    auto_market_scan()
    return "ok", 200


@app.route("/health")
def health():
    """Manual health check."""
    return {
        "status": "online",
        "session_active": is_session_active(),
        "session": get_session_label(),
        "last_scan": LAST_SCAN_TIME.isoformat() if LAST_SCAN_TIME else None,
        "version": "2.0-session1",
    }, 200


@app.route("/startup")
def startup():
    """Send the dashboard manually (for testing)."""
    if CHAT_ID:
        send_telegram(CHAT_ID, dashboard_message(), main_menu())
    return "ok", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """Main Telegram webhook handler."""
    data = request.json
    if not data:
        return "ok", 200

    # ─── Inline button presses ──────────────────────────────
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

        elif action == "analyze_gold" or action == "force_scan":
            label = "*Analyzing gold...*" if action == "analyze_gold" else "*Force scanning gold...*"
            send_telegram(chat_id, label)
            ok, result = run_gold_analysis()
            if ok:
                send_telegram(chat_id, result["body"], back_button())
            else:
                msg = f"{result['headline']}\n{DIVIDER}\n{result['body']}"
                send_telegram(chat_id, msg, back_button())

    # ─── Text/photo messages ────────────────────────────────
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
                f"☑ *Bot Health*\n{DIVIDER}\nVersion: `2.0-session1`\nSession: {get_session_label()}\n{heartbeat_line()}",
                back_button(),
            )
        elif text == "/rules":
            send_telegram(chat_id, rules_message(), back_button())
        elif text == "/checklist":
            send_telegram(chat_id, checklist_message(), back_button())
        else:
            # Unknown text — just direct to dashboard
            send_telegram(chat_id, "_Tap a button on the dashboard, or use_ `/menu`", back_button())

    return "ok", 200


# ─────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
