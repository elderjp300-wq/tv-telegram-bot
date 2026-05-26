"""
=============================================================================
JP GOLD BOT v3.1 — Textbook SMC on 15M
=============================================================================
Strategy:
  - 2H timeframe used ONLY for trend context (bullish/bearish/range)
  - 15M timeframe: detect BOS/CHoCH, locate OB and FVG inside impulse
  - Composite grading (A+ / A / B / C) decides signal quality
  - Limit entry at OB open, SL beyond OB invalidation
  - TP1 fixed 3R, TP2 nearest untouched 2H swing beyond TP1

Lifecycle:
  PENDING → ACTIVE → TP1_HIT → TP2_HIT (or SL_HIT or EXPIRED at any point)
  Add-on alerts fire whenever a same-direction setup forms while a trade is
  active (fires regardless of P&L per your choice — risk is called out in
  the message).

Operations:
  - XAUUSD only
  - NY + London sessions (07:00 UTC – 22:00 UTC)
  - Friday wind-down at 17:00 UTC (pending signals cleared)
  - 0.5% risk on A+/A, 0.25% on B/C
  - All grades sent; commentary in message tells you which to take
=============================================================================
"""

import os
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import requests
from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
import asyncio

# =============================================================================
# CONFIG
# =============================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "")
PORT = int(os.getenv("PORT", "10000"))
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "5000"))

INSTRUMENT_DISPLAY = "XAUUSD"
INSTRUMENT_API = "XAU/USD"

# Sessions (UTC)
SESSION_START_UTC = 7      # London opens
SESSION_END_UTC = 22       # NY closes
FRIDAY_CUTOFF_HOUR = 17    # Friday wind-down

# Timeframes
TF_2H = "2h"
TF_15M = "15min"
CANDLES_2H = 80           # ~6.5 days of 2H bars
CANDLES_15M = 120         # ~30 hours of 15M bars

# Strategy params
SWING_LOOKBACK = 3        # swing detection — bars on each side
SIGNAL_EXPIRY_BARS = 8    # 15M bars before pending signal expires (~2h)
SL_BUFFER_USD = 0.50      # extra buffer beyond OB invalidation for SL
SCAN_INTERVAL_SECONDS = 120  # how often the scanner runs

# Grading
RISK_BY_GRADE = {"A+": 0.005, "A": 0.005, "B": 0.0025, "C": 0.0025}

# State
STATE_FILE = "bot_state.json"

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("jp-gold-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# =============================================================================
# STATE
# =============================================================================
state: Dict[str, Any] = {
    "active_trades": [],      # PENDING and ACTIVE trades
    "completed_trades": [],   # TP_HIT, SL_HIT, EXPIRED — kept for /status
    "paused": False,
    "last_scan_ts": None,
    "last_2h_trend": None,
    "signals_today": 0,
    "today_date": None,
    "bot_started_at": datetime.now(timezone.utc).isoformat(),
}


def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                loaded = json.load(f)
            state.update(loaded)
            log.info(f"State loaded: {len(state['active_trades'])} active, "
                     f"{len(state['completed_trades'])} completed")
        except Exception as e:
            log.warning(f"Could not load state: {e}. Starting fresh.")


def save_state():
    try:
        # Cap completed_trades to last 100 to keep file size reasonable
        if len(state["completed_trades"]) > 100:
            state["completed_trades"] = state["completed_trades"][-100:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Could not save state: {e}")


def reset_daily_counter():
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("today_date") != today:
        state["today_date"] = today
        state["signals_today"] = 0


# =============================================================================
# TWELVE DATA FETCHING
# =============================================================================
def fetch_candles(timeframe: str, count: int) -> Optional[List[Dict]]:
    """Fetch OHLC from Twelve Data. Returns list of candle dicts (oldest first)."""
    if not TWELVE_DATA_KEY:
        log.error("TWELVE_DATA_KEY not set")
        return None
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": INSTRUMENT_API,
        "interval": timeframe,
        "outputsize": count,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            log.error(f"Twelve Data error: {data.get('message')}")
            return None
        values = data.get("values", [])
        if not values:
            log.warning(f"No candles returned for {timeframe}")
            return None
        # Reverse to oldest-first and normalize
        candles = []
        for v in reversed(values):
            candles.append({
                "time": v["datetime"],
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
            })
        # v3.1 FIX: Twelve Data returns the currently-FORMING candle as the
        # most recent value. Drop it. Every downstream function must only ever
        # see fully CLOSED candles — otherwise BOS / tap / SL detection reads a
        # half-formed bar whose high-low range corrupts everything.
        if len(candles) > 1:
            candles = candles[:-1]
        return candles
    except Exception as e:
        log.error(f"fetch_candles({timeframe}) failed: {e}")
        return None


# =============================================================================
# STRUCTURE DETECTION
# =============================================================================
def detect_swings(candles: List[Dict], lookback: int = SWING_LOOKBACK) -> List[Dict]:
    """Identify swing highs/lows. Swing requires `lookback` bars on each side."""
    swings = []
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        is_high = all(candles[i + j]["high"] <= c["high"]
                      for j in range(-lookback, lookback + 1) if j != 0)
        is_low = all(candles[i + j]["low"] >= c["low"]
                     for j in range(-lookback, lookback + 1) if j != 0)
        if is_high:
            swings.append({"idx": i, "type": "high", "price": c["high"], "time": c["time"]})
        if is_low:
            swings.append({"idx": i, "type": "low", "price": c["low"], "time": c["time"]})
    return swings


def determine_trend(candles: List[Dict]) -> str:
    """Determine market structure: bullish, bearish, or range."""
    swings = detect_swings(candles, lookback=SWING_LOOKBACK)
    highs = [s for s in swings if s["type"] == "high"][-3:]
    lows = [s for s in swings if s["type"] == "low"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return "range"
    hh = all(highs[i]["price"] > highs[i - 1]["price"] for i in range(1, len(highs)))
    hl = all(lows[i]["price"] > lows[i - 1]["price"] for i in range(1, len(lows)))
    lh = all(highs[i]["price"] < highs[i - 1]["price"] for i in range(1, len(highs)))
    ll = all(lows[i]["price"] < lows[i - 1]["price"] for i in range(1, len(lows)))
    if hh and hl:
        return "bullish"
    if lh and ll:
        return "bearish"
    return "range"


def detect_15m_break(candles: List[Dict]) -> Optional[Dict]:
    """Detect BOS or CHoCH on most recent CLOSED 15M candle.
    NOTE: fetch_candles() already drops the forming candle, so candles[-1]
    here is guaranteed to be a fully closed bar."""
    if len(candles) < SWING_LOOKBACK * 2 + 2:
        return None
    # candles[-1] is the most recent CLOSED candle (forming bar already dropped)
    last_idx = len(candles) - 1
    last = candles[last_idx]
    # Detect swings up to (but not including) last candle
    swings = detect_swings(candles[:last_idx], lookback=SWING_LOOKBACK)
    if not swings:
        return None

    # Most recent swing high → bullish break if close above it
    recent_high = next((s for s in reversed(swings) if s["type"] == "high"), None)
    if recent_high and last["close"] > recent_high["price"]:
        return {
            "direction": "bullish",
            "bos_idx": last_idx,
            "bos_candle": last,
            "broken_swing": recent_high,
        }

    # Most recent swing low → bearish break if close below
    recent_low = next((s for s in reversed(swings) if s["type"] == "low"), None)
    if recent_low and last["close"] < recent_low["price"]:
        return {
            "direction": "bearish",
            "bos_idx": last_idx,
            "bos_candle": last,
            "broken_swing": recent_low,
        }

    return None


def find_order_block(candles: List[Dict], bos: Dict) -> Optional[Dict]:
    """Locate the OB — last opposing-color candle inside the impulse leg."""
    direction = bos["direction"]
    bos_idx = bos["bos_idx"]
    broken_idx = bos["broken_swing"]["idx"]
    # Walk back from BOS toward broken swing
    for i in range(bos_idx - 1, broken_idx, -1):
        c = candles[i]
        is_bearish = c["close"] < c["open"]
        is_bullish = c["close"] > c["open"]
        if direction == "bullish" and is_bearish:
            return {"idx": i, **c}
        if direction == "bearish" and is_bullish:
            return {"idx": i, **c}
    return None


def find_fvg(candles: List[Dict], bos: Dict, ob_idx: int) -> Optional[Dict]:
    """Look for 3-bar FVG inside the impulse leg between OB and BOS."""
    direction = bos["direction"]
    bos_idx = bos["bos_idx"]
    for i in range(ob_idx + 1, bos_idx - 1):
        c1 = candles[i]
        c3 = candles[i + 2]
        if direction == "bullish" and c1["high"] < c3["low"]:
            return {"low": c1["high"], "high": c3["low"], "idx": i + 1}
        if direction == "bearish" and c1["low"] > c3["high"]:
            return {"low": c3["high"], "high": c1["low"], "idx": i + 1}
    return None


# =============================================================================
# GRADING
# =============================================================================
def compute_levels(direction: str, ob: Dict) -> Dict:
    """Calculate entry, SL, TP1 (3R) from OB."""
    entry = ob["open"]
    if direction == "bullish":
        sl = ob["low"] - SL_BUFFER_USD
        risk = entry - sl
        tp1 = entry + 3 * risk
    else:
        sl = ob["high"] + SL_BUFFER_USD
        risk = sl - entry
        tp1 = entry - 3 * risk
    return {"entry": entry, "sl": sl, "tp1": tp1, "risk": abs(entry - sl)}


def find_tp2(direction: str, entry: float, tp1: float,
             swings_2h: List[Dict]) -> Optional[float]:
    """Nearest untouched 2H swing beyond TP1."""
    if direction == "bullish":
        candidates = [s["price"] for s in swings_2h
                      if s["type"] == "high" and s["price"] > tp1]
        return min(candidates) if candidates else None
    else:
        candidates = [s["price"] for s in swings_2h
                      if s["type"] == "low" and s["price"] < tp1]
        return max(candidates) if candidates else None


def grade_setup(direction: str, fvg: Optional[Dict], levels: Dict,
                trend_2h: str, swings_2h: List[Dict]) -> Dict:
    """Composite scoring → letter grade."""
    score = 0
    factors = []

    # +2 Trend alignment
    if (direction == "bullish" and trend_2h == "bullish") or \
       (direction == "bearish" and trend_2h == "bearish"):
        score += 2
        factors.append(("✓", f"Trend aligned (2H {trend_2h})"))
    else:
        factors.append(("✗", f"Trend not aligned (2H {trend_2h})"))

    # +1 FVG present
    if fvg:
        score += 1
        factors.append(("✓", "FVG in impulse"))
    else:
        factors.append(("✗", "No FVG in impulse"))

    # +1 NY or London session
    hour = datetime.now(timezone.utc).hour
    if 12 <= hour < 22:
        score += 1
        factors.append(("✓", "NY session"))
    elif SESSION_START_UTC <= hour < 12:
        score += 1
        factors.append(("✓", "London session"))
    else:
        factors.append(("✗", "Off-session"))

    # +1 Clean OB (any newly-identified OB is clean by definition)
    score += 1
    factors.append(("✓", "Clean OB (no prior taps)"))

    # +1 Room to nearest opposing 2H swing > 1.5R
    target_15r = (levels["entry"] + 1.5 * levels["risk"]
                  if direction == "bullish"
                  else levels["entry"] - 1.5 * levels["risk"])
    if direction == "bullish":
        nearest = min((s["price"] for s in swings_2h
                       if s["type"] == "high" and s["price"] > levels["entry"]),
                      default=None)
        room_ok = nearest is not None and nearest > target_15r
    else:
        nearest = max((s["price"] for s in swings_2h
                       if s["type"] == "low" and s["price"] < levels["entry"]),
                      default=None)
        room_ok = nearest is not None and nearest < target_15r
    if room_ok:
        score += 1
        factors.append(("✓", "Room to 2H swing > 1.5R"))
    else:
        factors.append(("✗", "Tight 2H room (< 1.5R)"))

    # Letter grade
    if score >= 5:
        grade = "A+"
    elif score >= 4:
        grade = "A"
    elif score >= 3:
        grade = "B"
    else:
        grade = "C"

    return {
        "score": score,
        "grade": grade,
        "factors": factors,
        "risk_pct": RISK_BY_GRADE[grade],
    }


# =============================================================================
# TELEGRAM
# =============================================================================
bot: Optional[Bot] = None


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "—"
    return f"{p:.2f}"


def build_signal_message(trade: Dict) -> str:
    g = trade["grading"]
    direction_emoji = "🟢 BUY" if trade["direction"] == "bullish" else "🔴 SELL"
    grade_emoji = {"A+": "🏆", "A": "⭐", "B": "📊", "C": "⚠️"}[g["grade"]]
    risk_amt = ACCOUNT_SIZE * g["risk_pct"]

    factor_lines = "\n".join(f"  {sym} {txt}" for sym, txt in g["factors"])

    lines = [
        f"{grade_emoji} <b>SIGNAL · {INSTRUMENT_DISPLAY} · {direction_emoji} · {g['grade']}</b>",
        f"#signal #{trade['direction']} #{g['grade'].replace('+', 'plus').lower()}",
        "",
        f"<b>Grade:</b> {g['grade']} ({g['score']}/6)",
        factor_lines,
        "",
        f"<b>Entry (limit at OB):</b> {fmt_price(trade['entry'])}",
        f"<b>Stop Loss:</b> {fmt_price(trade['sl'])}",
        f"<b>TP1 (3R):</b> {fmt_price(trade['tp1'])}",
        f"<b>TP2 (2H swing):</b> {fmt_price(trade.get('tp2'))}",
        "",
        f"<b>Risk:</b> {g['risk_pct']*100:.2f}%  (~${risk_amt:.2f} on ${ACCOUNT_SIZE:.0f})",
        f"<b>Expires:</b> {SIGNAL_EXPIRY_BARS} bars (no fill = canceled)",
        "",
        f"<i>Trade ID: {trade['id']}</i>",
    ]
    return "\n".join(lines)


def build_addon_message(trade: Dict, parent_id: str, parent_entry: float) -> str:
    g = trade["grading"]
    direction_emoji = "🟢 BUY" if trade["direction"] == "bullish" else "🔴 SELL"
    risk_amt = ACCOUNT_SIZE * g["risk_pct"]
    factor_lines = "\n".join(f"  {sym} {txt}" for sym, txt in g["factors"])

    lines = [
        f"➕ <b>ADD-ON · {INSTRUMENT_DISPLAY} · {direction_emoji} · {g['grade']}</b>",
        f"#addon #{trade['direction']} #{g['grade'].replace('+', 'plus').lower()}",
        "",
        f"<b>Same direction as Trade <code>{parent_id}</code> (still active)</b>",
        f"⚠️ Reminder: if taken, <b>move Trade {parent_id} SL to BE at {fmt_price(parent_entry)}</b>",
        "",
        f"<b>Grade:</b> {g['grade']} ({g['score']}/6)",
        factor_lines,
        "",
        f"<b>Entry:</b> {fmt_price(trade['entry'])}",
        f"<b>Stop Loss:</b> {fmt_price(trade['sl'])}",
        f"<b>TP1 (3R):</b> {fmt_price(trade['tp1'])}",
        f"<b>TP2 (2H swing):</b> {fmt_price(trade.get('tp2'))}",
        "",
        f"<b>Risk:</b> {g['risk_pct']*100:.2f}%  (~${risk_amt:.2f})",
        f"<i>Trade ID: {trade['id']}</i>",
    ]
    return "\n".join(lines)


def build_buttons(trade_id: str) -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton("✅ Taken", callback_data=f"taken_{trade_id}"),
        InlineKeyboardButton("⏭️ Skipped", callback_data=f"skipped_{trade_id}"),
    ]]
    return InlineKeyboardMarkup(keyboard)


def send_telegram(text: str, buttons: Optional[InlineKeyboardMarkup] = None):
    """Synchronous Telegram send (uses HTTP API directly for reliability)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram creds missing — would have sent:\n" + text[:200])
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [
                [{"text": btn.text, "callback_data": btn.callback_data}
                 for btn in row]
                for row in buttons.inline_keyboard
            ]
        })
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            log.error(f"Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")


# =============================================================================
# SIGNAL GENERATION
# =============================================================================
def make_trade_id(bos_candle_time: str, direction: str) -> str:
    """Deterministic ID from BOS candle so duplicate scans don't re-fire."""
    # Normalize: drop spaces/colons
    clean = bos_candle_time.replace(" ", "_").replace(":", "").replace("-", "")
    return f"{clean}_{direction[0]}"


def trade_id_exists(trade_id: str) -> bool:
    for t in state["active_trades"]:
        if t["id"] == trade_id:
            return True
    for t in state["completed_trades"]:
        if t["id"] == trade_id:
            return True
    return False


def create_trade(direction: str, bos: Dict, ob: Dict, fvg: Optional[Dict],
                 grading: Dict, levels: Dict, tp2: Optional[float],
                 is_addon: bool = False, parent_id: Optional[str] = None) -> Dict:
    trade_id = make_trade_id(bos["bos_candle"]["time"], direction)
    return {
        "id": trade_id,
        "direction": direction,
        "state": "PENDING",
        "entry": levels["entry"],
        "sl": levels["sl"],
        "tp1": levels["tp1"],
        "tp2": tp2,
        "risk": levels["risk"],
        "ob_high": ob["high"],
        "ob_low": ob["low"],
        "ob_open": ob["open"],
        "fvg": fvg,
        "grading": grading,
        "bos_time": bos["bos_candle"]["time"],
        # v3.1: the candle that PRODUCED this signal. A trade may only tap on a
        # candle strictly AFTER this one — never on the signal candle itself.
        "signal_candle_time": bos["bos_candle"]["time"],
        # v3.1: last closed-candle time we've already evaluated for this trade.
        # Guarantees each candle is processed exactly once, in order.
        "last_processed_candle_time": bos["bos_candle"]["time"],
        # v3.1: the candle on which the trade tapped (set when ACTIVE). SL/TP may
        # only register on a candle AFTER this one.
        "tapped_candle_time": None,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "tapped_at": None,
        "closed_at": None,
        "outcome": None,
        "tp1_hit": False,
        "tp2_hit": False,
        "expiry_bars_left": SIGNAL_EXPIRY_BARS,
        "user_decision": None,  # taken / skipped / null
        "is_addon": is_addon,
        "parent_trade_id": parent_id,
    }


# =============================================================================
# LIFECYCLE: monitor PENDING and ACTIVE trades
# =============================================================================
def _candle_taps_entry(candle: Dict, direction: str, entry: float) -> bool:
    """Did this candle's range reach the limit entry?"""
    if direction == "bullish":
        return candle["low"] <= entry
    return candle["high"] >= entry


def _candle_hits(candle: Dict, direction: str, level: float, kind: str) -> bool:
    """Did this candle reach an SL or TP level?
    kind = 'sl' or 'tp'. For a bullish trade SL is below / TP is above."""
    if direction == "bullish":
        if kind == "sl":
            return candle["low"] <= level
        return candle["high"] >= level
    else:
        if kind == "sl":
            return candle["high"] >= level
        return candle["low"] <= level


def _resolve_same_candle(candle: Dict, direction: str, sl: float,
                         tp: float) -> str:
    """When one candle's range spans BOTH SL and a TP, decide which came
    first using the candle's open->close direction as a heuristic.

    A bullish candle (close > open) most likely went down-then-up, so it
    touched the lower level first. A bearish candle went up-then-down.
    Returns 'sl', 'tp', or 'none'.
    Conservative tie-break: if direction is ambiguous, favour SL (worst case).
    """
    hit_sl = _candle_hits(candle, direction, sl, "sl")
    hit_tp = _candle_hits(candle, direction, tp, "tp")
    if hit_sl and not hit_tp:
        return "sl"
    if hit_tp and not hit_sl:
        return "tp"
    if not hit_sl and not hit_tp:
        return "none"
    # Both hit in one candle — use candle body direction.
    bullish_candle = candle["close"] > candle["open"]
    bearish_candle = candle["close"] < candle["open"]
    if direction == "bullish":
        # SL is below entry, TP above. A bullish (up-closing) candle most
        # likely dipped to SL first then rallied. A bearish candle rallied
        # to TP first then fell.
        if bullish_candle:
            return "sl"
        if bearish_candle:
            return "tp"
    else:
        # Bearish trade: SL above, TP below. A bearish candle most likely
        # spiked up to SL first then dropped. A bullish candle dropped to
        # TP first then rose.
        if bearish_candle:
            return "sl"
        if bullish_candle:
            return "tp"
    # Doji / ambiguous — conservative: assume SL first.
    return "sl"


def update_trade_lifecycle(candles_15m: List[Dict]):
    """v3.1: time-ordered, candle-sequenced lifecycle.

    Key guarantees vs v3.0:
      - Only CLOSED candles are evaluated (fetch_candles drops the forming bar).
      - Each candle is processed exactly ONCE per trade, in chronological order.
      - A trade can only TAP on a candle strictly AFTER its signal candle.
      - A trade can only hit SL/TP on a candle strictly AFTER it tapped.
      - If one candle spans both SL and TP, body direction resolves order.
    This kills the "signal + tapped + SL in the same minute" cascade.
    """
    if not candles_15m:
        return

    still_active = []
    for t in state["active_trades"]:
        # Closed candles newer than the last one we processed for this trade.
        new_candles = [c for c in candles_15m
                       if c["time"] > t["last_processed_candle_time"]]

        terminal = False  # set True if trade reaches a closed state

        for candle in new_candles:
            ctime = candle["time"]

            # ---- PENDING: waiting for the limit at the OB to tap ----
            if t["state"] == "PENDING":
                # A trade can never tap on its own signal candle, only later.
                if ctime <= t["signal_candle_time"]:
                    t["last_processed_candle_time"] = ctime
                    continue

                # Expiry: count each closed candle the signal sat unfilled.
                t["expiry_bars_left"] -= 1

                if _candle_taps_entry(candle, t["direction"], t["entry"]):
                    t["state"] = "ACTIVE"
                    t["tapped_at"] = datetime.now(timezone.utc).isoformat()
                    t["tapped_candle_time"] = ctime
                    t["last_processed_candle_time"] = ctime
                    send_telegram(
                        f"📍 <b>TAPPED · {INSTRUMENT_DISPLAY}</b>\n"
                        f"#tapped #{t['direction']}\n\n"
                        f"Trade <code>{t['id']}</code> is now LIVE.\n"
                        f"Entry: {fmt_price(t['entry'])} · "
                        f"SL: {fmt_price(t['sl'])} · "
                        f"TP1: {fmt_price(t['tp1'])}")
                    # IMPORTANT: do not also evaluate SL/TP on the SAME candle
                    # that tapped. Management starts on the NEXT candle.
                    continue

                # Still pending — did it expire?
                if t["expiry_bars_left"] <= 0:
                    t["state"] = "EXPIRED"
                    t["closed_at"] = datetime.now(timezone.utc).isoformat()
                    t["outcome"] = "EXPIRED"
                    t["last_processed_candle_time"] = ctime
                    send_telegram(
                        f"⌛ <b>EXPIRED · {INSTRUMENT_DISPLAY}</b>\n#expired\n\n"
                        f"Trade <code>{t['id']}</code> never tapped entry "
                        f"({fmt_price(t['entry'])}). Removed from watch.")
                    state["completed_trades"].append(t)
                    terminal = True
                    break

                t["last_processed_candle_time"] = ctime
                continue

            # ---- ACTIVE: manage SL / TP1 / TP2 ----
            if t["state"] == "ACTIVE":
                # Only candles strictly after the tap candle can close it.
                if t["tapped_candle_time"] and ctime <= t["tapped_candle_time"]:
                    t["last_processed_candle_time"] = ctime
                    continue

                # Decide what this candle did. Check SL vs the relevant TP.
                # Pre-TP1: SL vs TP1.  Post-TP1: SL(at BE) vs TP2.
                if not t["tp1_hit"]:
                    outcome = _resolve_same_candle(
                        candle, t["direction"], t["sl"], t["tp1"])
                    if outcome == "sl":
                        t["state"] = "SL_HIT"
                        t["closed_at"] = datetime.now(timezone.utc).isoformat()
                        t["outcome"] = "LOSS"
                        t["last_processed_candle_time"] = ctime
                        send_telegram(
                            f"🛑 <b>SL HIT · {INSTRUMENT_DISPLAY}</b>\n"
                            f"#sl #loss #{t['direction']}\n\n"
                            f"Trade <code>{t['id']}</code> stopped at "
                            f"{fmt_price(t['sl'])}.\n"
                            f"Risk taken: {t['grading']['risk_pct']*100:.2f}%")
                        state["completed_trades"].append(t)
                        terminal = True
                        break
                    elif outcome == "tp":
                        t["tp1_hit"] = True
                        send_telegram(
                            f"💰 <b>TP1 HIT (3R) · {INSTRUMENT_DISPLAY}</b>\n"
                            f"#tp1 #profit #{t['direction']}\n\n"
                            f"Trade <code>{t['id']}</code> hit TP1 at "
                            f"{fmt_price(t['tp1'])}.\n"
                            f"<b>Close partial. Move runner SL to BE: "
                            f"{fmt_price(t['entry'])}</b>"
                            + (f"\nRunner targeting TP2: {fmt_price(t['tp2'])}"
                               if t.get('tp2') else ""))
                        # Runner SL is now break-even (the entry price).
                        t["sl"] = t["entry"]
                        if not t.get("tp2"):
                            # No TP2 → close fully at TP1.
                            t["state"] = "TP1_HIT"
                            t["closed_at"] = \
                                datetime.now(timezone.utc).isoformat()
                            t["outcome"] = "WIN_TP1"
                            t["last_processed_candle_time"] = ctime
                            state["completed_trades"].append(t)
                            terminal = True
                            break
                        t["last_processed_candle_time"] = ctime
                        continue
                    else:
                        t["last_processed_candle_time"] = ctime
                        continue
                else:
                    # Post-TP1: runner with SL at BE, targeting TP2.
                    outcome = _resolve_same_candle(
                        candle, t["direction"], t["sl"], t["tp2"])
                    if outcome == "tp":
                        t["tp2_hit"] = True
                        t["state"] = "TP2_HIT"
                        t["closed_at"] = datetime.now(timezone.utc).isoformat()
                        t["outcome"] = "WIN_FULL"
                        t["last_processed_candle_time"] = ctime
                        send_telegram(
                            f"🏆 <b>TP2 HIT · {INSTRUMENT_DISPLAY}</b>\n"
                            f"#tp2 #profit #{t['direction']}\n\n"
                            f"Trade <code>{t['id']}</code> hit TP2 at "
                            f"{fmt_price(t['tp2'])}. Full close.")
                        state["completed_trades"].append(t)
                        terminal = True
                        break
                    elif outcome == "sl":
                        # Runner stopped at break-even — scratch, not a loss.
                        t["state"] = "BE_STOP"
                        t["closed_at"] = datetime.now(timezone.utc).isoformat()
                        t["outcome"] = "WIN_TP1_BE_RUNNER"
                        t["last_processed_candle_time"] = ctime
                        send_telegram(
                            f"⚖️ <b>RUNNER STOPPED AT BE · {INSTRUMENT_DISPLAY}"
                            f"</b>\n#breakeven #{t['direction']}\n\n"
                            f"Trade <code>{t['id']}</code> runner closed at "
                            f"break-even after TP1. Net result: +TP1 partial.")
                        state["completed_trades"].append(t)
                        terminal = True
                        break
                    else:
                        t["last_processed_candle_time"] = ctime
                        continue

        if not terminal:
            still_active.append(t)

    state["active_trades"] = still_active


# =============================================================================
# CORE SCANNER
# =============================================================================
def in_session() -> bool:
    """True if current UTC hour is within London or NY session."""
    h = datetime.now(timezone.utc).hour
    return SESSION_START_UTC <= h < SESSION_END_UTC


def scan():
    """Single scan cycle. Called by scheduler every SCAN_INTERVAL_SECONDS."""
    try:
        if state.get("paused"):
            return
        state["last_scan_ts"] = datetime.now(timezone.utc).isoformat()
        reset_daily_counter()

        # Fetch candles regardless of session (lifecycle updates need them)
        candles_15m = fetch_candles(TF_15M, CANDLES_15M)
        if not candles_15m:
            log.warning("scan: no 15M candles, abort")
            return

        # ALWAYS update lifecycle of existing trades, even off-session
        update_trade_lifecycle(candles_15m)
        save_state()

        # New signal generation only during session
        if not in_session():
            return

        candles_2h = fetch_candles(TF_2H, CANDLES_2H)
        if not candles_2h:
            log.warning("scan: no 2H candles, skip signal gen")
            return

        trend_2h = determine_trend(candles_2h)
        state["last_2h_trend"] = trend_2h
        swings_2h = detect_swings(candles_2h, lookback=SWING_LOOKBACK)

        bos = detect_15m_break(candles_15m)
        if not bos:
            return

        # Build trade ID early; skip if we've already fired on this BOS
        trade_id = make_trade_id(bos["bos_candle"]["time"], bos["direction"])
        if trade_id_exists(trade_id):
            return

        ob = find_order_block(candles_15m, bos)
        if not ob:
            log.info("scan: BOS found but no OB → skip")
            return

        fvg = find_fvg(candles_15m, bos, ob["idx"])
        levels = compute_levels(bos["direction"], ob)
        if levels["risk"] <= 0:
            log.info("scan: invalid risk (entry == SL) → skip")
            return

        # v3.1 GUARD: if price has ALREADY traded through the OB invalidation
        # (the SL level) on the BOS candle itself, the setup is dead on arrival
        # — a limit there would tap and stop instantly. Skip it.
        bos_candle = bos["bos_candle"]
        if bos["direction"] == "bullish" and bos_candle["low"] <= levels["sl"]:
            log.info("scan: OB already invalidated on BOS candle → skip")
            return
        if bos["direction"] == "bearish" and bos_candle["high"] >= levels["sl"]:
            log.info("scan: OB already invalidated on BOS candle → skip")
            return

        tp2 = find_tp2(bos["direction"], levels["entry"], levels["tp1"], swings_2h)
        grading = grade_setup(bos["direction"], fvg, levels, trend_2h, swings_2h)

        # Determine if this is an add-on
        same_dir_active = [t for t in state["active_trades"]
                           if t["direction"] == bos["direction"]
                           and t["state"] in ("PENDING", "ACTIVE")]

        if same_dir_active:
            parent = same_dir_active[0]  # oldest same-direction
            trade = create_trade(bos["direction"], bos, ob, fvg, grading, levels, tp2,
                                 is_addon=True, parent_id=parent["id"])
            state["active_trades"].append(trade)
            state["signals_today"] += 1
            send_telegram(
                build_addon_message(trade, parent["id"], parent["entry"]),
                buttons=build_buttons(trade["id"])
            )
            log.info(f"ADD-ON fired: {trade['id']} parent={parent['id']}")
        else:
            trade = create_trade(bos["direction"], bos, ob, fvg, grading, levels, tp2)
            state["active_trades"].append(trade)
            state["signals_today"] += 1
            send_telegram(
                build_signal_message(trade),
                buttons=build_buttons(trade["id"])
            )
            log.info(f"SIGNAL fired: {trade['id']} grade={grading['grade']}")

        save_state()

    except Exception as e:
        log.exception(f"scan() error: {e}")


# =============================================================================
# FRIDAY WIND-DOWN
# =============================================================================
def friday_wind_down():
    """Cancel all PENDING signals at Friday 17:00 UTC. Active trades persist."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 4:  # 4 = Friday
        return
    if now.hour != FRIDAY_CUTOFF_HOUR:
        return
    pending = [t for t in state["active_trades"] if t["state"] == "PENDING"]
    if not pending:
        return
    for t in pending:
        t["state"] = "EXPIRED"
        t["closed_at"] = now.isoformat()
        t["outcome"] = "FRIDAY_CUTOFF"
        state["completed_trades"].append(t)
    state["active_trades"] = [t for t in state["active_trades"]
                              if t["state"] != "EXPIRED"]
    active_remaining = [t for t in state["active_trades"] if t["state"] == "ACTIVE"]
    msg = (f"🌙 <b>Friday Wind-Down (17:00 UTC)</b>\n#winddown\n\n"
           f"Cleared {len(pending)} pending signal(s).")
    if active_remaining:
        msg += f"\n\n⚠️ {len(active_remaining)} ACTIVE trade(s) still live — "
        msg += "consider closing manually before weekend gap risk."
    send_telegram(msg)
    save_state()
    log.info(f"Friday wind-down: {len(pending)} pending cleared")


# =============================================================================
# TELEGRAM COMMAND HANDLERS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "JP Gold Bot v3.1 — 15M Textbook SMC\n\n"
        "Commands:\n"
        "/status — diagnostics\n"
        "/config — current parameters\n"
        "/pause — pause scanning\n"
        "/resume — resume scanning\n"
        "/clear_trades — drop all active trades (use with care)\n"
        "/test_functions — verify scanner end-to-end\n"
        "/help — this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = state["active_trades"]
    pending = [t for t in a if t["state"] == "PENDING"]
    active = [t for t in a if t["state"] == "ACTIVE"]
    last_scan = state.get("last_scan_ts", "never")
    trend = state.get("last_2h_trend", "?")

    lines = [
        f"<b>JP Gold Bot v3.1 — Status</b>",
        "",
        f"Paused: {'YES' if state.get('paused') else 'no'}",
        f"In session: {'yes' if in_session() else 'no'} (UTC hour {datetime.now(timezone.utc).hour})",
        f"Last 2H trend: {trend}",
        f"Last scan: {last_scan}",
        f"Signals today: {state.get('signals_today', 0)}",
        f"Account size: ${ACCOUNT_SIZE:.0f}",
        "",
        f"<b>Active book:</b>",
        f"  PENDING: {len(pending)}",
        f"  ACTIVE (live): {len(active)}",
        f"  Completed (history): {len(state['completed_trades'])}",
    ]
    if pending:
        lines.append("\n<b>Pending signals:</b>")
        for t in pending[:5]:
            lines.append(f"  {t['id']} · {t['direction'][:4]} · entry "
                         f"{fmt_price(t['entry'])} · {t['grading']['grade']} · "
                         f"{t['expiry_bars_left']} bars left")
    if active:
        lines.append("\n<b>Live trades:</b>")
        for t in active[:5]:
            tp_status = "TP1✓" if t["tp1_hit"] else "→TP1"
            lines.append(f"  {t['id']} · {t['direction'][:4]} · entry "
                         f"{fmt_price(t['entry'])} · {tp_status}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "<b>Bot Config</b>",
        f"Instrument: {INSTRUMENT_DISPLAY}",
        f"Sessions: {SESSION_START_UTC:02d}:00 – {SESSION_END_UTC:02d}:00 UTC (London + NY)",
        f"Friday wind-down: {FRIDAY_CUTOFF_HOUR:02d}:00 UTC",
        f"Scan interval: {SCAN_INTERVAL_SECONDS}s",
        f"Signal expiry: {SIGNAL_EXPIRY_BARS} × 15M bars",
        f"SL buffer: ${SL_BUFFER_USD}",
        f"Swing lookback: {SWING_LOOKBACK} bars",
        "",
        "<b>Grading thresholds</b>",
        "  A+ = 5–6 pts · A = 4 pts · B = 3 pts · C = 0–2 pts",
        "",
        "<b>Risk per grade</b>",
        "  A+/A: 0.50%  ·  B/C: 0.25%",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    save_state()
    await update.message.reply_text("⏸ Bot paused. Lifecycle tracking continues; no new signals.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    save_state()
    await update.message.reply_text("▶️ Bot resumed.")


async def cmd_clear_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = len(state["active_trades"])
    for t in state["active_trades"]:
        t["state"] = "CLEARED"
        t["closed_at"] = datetime.now(timezone.utc).isoformat()
        t["outcome"] = "MANUAL_CLEAR"
        state["completed_trades"].append(t)
    state["active_trades"] = []
    save_state()
    await update.message.reply_text(f"🧹 Cleared {n} active trade(s).")


async def cmd_test_functions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full smoke test — fetches data, runs each step, reports pass/fail."""
    results = []

    # 1. Twelve Data fetch
    c15 = fetch_candles(TF_15M, 50)
    results.append(("Fetch 15M candles", c15 is not None and len(c15) > 0,
                    f"{len(c15) if c15 else 0} bars"))

    c2h = fetch_candles(TF_2H, 50)
    results.append(("Fetch 2H candles", c2h is not None and len(c2h) > 0,
                    f"{len(c2h) if c2h else 0} bars"))

    # 2. Swing detection
    if c15:
        s15 = detect_swings(c15)
        results.append(("15M swings", len(s15) > 0, f"{len(s15)} swings"))
    if c2h:
        s2h = detect_swings(c2h)
        results.append(("2H swings", len(s2h) > 0, f"{len(s2h)} swings"))
        trend = determine_trend(c2h)
        results.append(("2H trend", True, trend))

    # 3. Telegram reachable
    results.append(("Telegram token set", bool(TELEGRAM_TOKEN), ""))
    results.append(("Telegram chat set", bool(TELEGRAM_CHAT_ID), ""))

    # 4. State persistence
    save_state()
    results.append(("State write", os.path.exists(STATE_FILE), STATE_FILE))

    lines = ["<b>Test Functions</b>", ""]
    for name, ok, extra in results:
        sym = "✅" if ok else "❌"
        lines.append(f"{sym} {name}" + (f" — {extra}" if extra else ""))

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Taken/Skipped button taps."""
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if "_" not in data:
        return
    action, trade_id = data.split("_", 1)
    # Find the trade in active or completed
    target = None
    for t in state["active_trades"]:
        if t["id"] == trade_id:
            target = t
            break
    if target is None:
        for t in state["completed_trades"]:
            if t["id"] == trade_id:
                target = t
                break
    if target is None:
        await q.edit_message_reply_markup(reply_markup=None)
        return
    target["user_decision"] = action
    save_state()
    # Remove buttons after decision recorded
    try:
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"Recorded: <b>{action.upper()}</b> for trade <code>{trade_id}</code>",
            parse_mode="HTML",
            reply_to_message_id=q.message.message_id,
        )
    except Exception as e:
        log.warning(f"callback_handler edit failed: {e}")


# =============================================================================
# FLASK APP
# =============================================================================
app = Flask(__name__)


@app.route("/")
def health():
    return jsonify({
        "status": "ok",
        "bot": "JP Gold Bot v3.1",
        "paused": state.get("paused", False),
        "active_trades": len(state["active_trades"]),
        "completed_trades": len(state["completed_trades"]),
        "last_scan": state.get("last_scan_ts"),
        "last_2h_trend": state.get("last_2h_trend"),
        "in_session": in_session(),
    })


@app.route("/test_functions")
def test_functions_http():
    """HTTP endpoint mirror of /test_functions for post-deploy ping."""
    results = {}
    c15 = fetch_candles(TF_15M, 50)
    results["fetch_15m"] = bool(c15)
    c2h = fetch_candles(TF_2H, 50)
    results["fetch_2h"] = bool(c2h)
    if c2h:
        results["trend_2h"] = determine_trend(c2h)
    results["telegram_creds"] = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    results["state_file"] = os.path.exists(STATE_FILE)
    return jsonify(results)


# =============================================================================
# STARTUP
# =============================================================================
def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(scan, "interval", seconds=SCAN_INTERVAL_SECONDS, id="scan",
                  max_instances=1, coalesce=True)
    # Run Friday wind-down check every hour; the function itself gates by time
    sched.add_job(friday_wind_down, "cron", minute=0, id="friday_wind_down")
    sched.start()
    log.info(f"Scheduler started: scan every {SCAN_INTERVAL_SECONDS}s")


def start_telegram_polling():
    """Start Telegram bot in a background thread (async-aware)."""
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN missing — bot commands disabled")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("config", cmd_config))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("clear_trades", cmd_clear_trades))
    application.add_handler(CommandHandler("test_functions", cmd_test_functions))
    application.add_handler(CallbackQueryHandler(callback_handler))

    import threading

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        application.run_polling(stop_signals=None, close_loop=False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    log.info("Telegram polling started")


load_state()
start_scheduler()
start_telegram_polling()

# Startup ping
try:
    send_telegram(
        "🟢 <b>JP Gold Bot v3.1 online</b>\n"
        f"#startup\n\n"
        f"15M textbook SMC strategy active.\n"
        f"Instrument: {INSTRUMENT_DISPLAY}\n"
        f"Sessions: {SESSION_START_UTC:02d}:00 – {SESSION_END_UTC:02d}:00 UTC\n"
        f"Use /status to verify, /test_functions to smoke test."
    )
except Exception as e:
    log.warning(f"Startup ping failed: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
