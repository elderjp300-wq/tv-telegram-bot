"""
═══════════════════════════════════════════════════════════════════════
  JP GOLD BOT — v2.0 (Sessions 1+2+3: Production Build)
═══════════════════════════════════════════════════════════════════════
  Built for: Johnpaul Uche
  Strategy:  Gold-only, 2H structure + 15M trigger, SMC/ICT
  Stack:     Flask + Telegram + Twelve Data (no pandas/numpy)

  WHAT THIS BOT DOES (end-to-end):
    1.  Watches gold 2H structure during London/NY sessions
    2.  Detects fresh 2H BOS on gold
    3.  Marks the Order Block (last opposing candle before impulse)
    4.  Marks the FVG/imbalance in the impulse leg (if present)
    5.  Sends zone to user with Valid/Poor buttons
    6.  If user marks Valid -> bot saves zone in memory + Telegram log
    7.  Bot watches price proximity to zone (Approaching/Near/Tapped)
    8.  Bot watches 15M for BOS/CHoCH at the zone
    9.  When 15M trigger fires inside zone -> SIGNAL FIRES with entry/SL/TP
   10.  DXY confluence check (must move opposite to trade direction)
   11.  Auto-invalidation when zone fails or signal completes
   12.  Anti-spam: each alert state fires once per zone

  USER INTERFACE:
    - Single back-button under analysis (no menu stuffing)
    - Heartbeat indicator on dashboard
    - Inline buttons for zone validation
    - Hashtag-based Telegram log (#zone #signal #log)
═══════════════════════════════════════════════════════════════════════
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request

# ─────────────────────────────────────────────────────────────
# CONFIG & ENVIRONMENT
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)

BOT_TOKEN        = os.environ.get("BOT_TOKEN")
CHAT_ID          = os.environ.get("CHAT_ID")
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")  # optional, used for chat

# Strategy parameters
SWING_LOOKBACK       = 2         # bars on each side (lowered from 5; tested for 2H gold)
ATR_PERIOD           = 14
ATR_SIGNIFICANCE     = 0.5       # swing must clear 0.5*ATR
CONSOLIDATION_RATIO  = 1.5       # range > 1.5*ATR to trade
MIN_RR               = 3.0
RISK_PERCENT         = 0.5

# Proximity thresholds (multiples of 2H ATR)
APPROACH_ATR         = 1.5       # within 1.5*ATR of zone -> APPROACHING
NEAR_ATR             = 0.5       # within 0.5*ATR of zone -> NEAR

# 15M strategy params (smaller lookback for faster response)
LTF_SWING_LOOKBACK   = 3
LTF_ATR_PERIOD       = 14

# Session windows (UTC)
LONDON_OPEN, LONDON_CLOSE = 7, 12
NY_OPEN, NY_CLOSE         = 12, 17

# ─────────────────────────────────────────────────────────────
# GLOBAL STATE (in-memory)
# ─────────────────────────────────────────────────────────────
LAST_SCAN_TIME = None
LAST_2H_SCAN_TIME = None
LAST_15M_SCAN_TIME = None
LAST_DXY_CHECK = None         # dict: {time, trend, price}
LAST_2H_BOS_LEVEL = None      # to dedupe BOS alerts
LAST_2H_BOS_DIRECTION = None  # tracks if direction has flipped
LAST_SESSION_OPEN_NOTIFIED = None  # date string of last session-open ping
LAST_FRIDAY_WINDDOWN = None   # date string of last Friday winddown
LAST_ERROR = None             # dict: {time, where, message}
ALERTS_SENT_TODAY = 0
SIGNALS_SENT_TODAY = 0
ALERTS_DAY = None             # date string for daily counter reset

# Track BOS levels we've already proposed (so we don't re-propose
# the same setup on every scan). Reset when direction flips.
PROPOSED_BOS_LEVELS = set()

# Friday wind-down hour (UTC)
FRIDAY_WINDDOWN_HOUR = 15     # no new signals after 15:00 UTC Friday

# Active zones: list of dicts. Each zone:
#   {
#     "id":           "z_2026-05-01_11-30",
#     "direction":    "bullish" | "bearish",
#     "ob_high":      4605.0,
#     "ob_low":       4598.0,
#     "fvg_high":     4603.0 or None,
#     "fvg_low":      4596.0 or None,
#     "created_at":   isoformat string,
#     "state":        "PENDING" | "VALIDATED" | "APPROACHING" | "NEAR" | "TAPPED" | "DONE"
#     "alerts_sent":  set of state names already alerted
#     "bos_level":    the 2H BOS price that triggered this zone
#   }
ACTIVE_ZONES = []

# Pending zones awaiting user validation (keyed by zone id)
PENDING_ZONES = {}

# Pending edit prompts (when user clicked Edit on a zone)
EDIT_PROMPTS = {}  # chat_id -> zone_id

MAX_ACTIVE_ZONES = 2  # cap


# ─────────────────────────────────────────────────────────────
# OBSERVABILITY HELPERS
# ─────────────────────────────────────────────────────────────
def log_error(where, message):
    """Record an error for /status visibility."""
    global LAST_ERROR
    LAST_ERROR = {
        "time":    now_utc().isoformat(),
        "where":   where,
        "message": str(message)[:200],  # truncate massive messages
    }


def reset_daily_counters_if_needed():
    """Reset alert/signal counters at the start of each new UTC day."""
    global ALERTS_SENT_TODAY, SIGNALS_SENT_TODAY, ALERTS_DAY
    today = now_utc().strftime("%Y-%m-%d")
    if ALERTS_DAY != today:
        ALERTS_DAY = today
        ALERTS_SENT_TODAY = 0
        SIGNALS_SENT_TODAY = 0


def is_friday_winddown():
    """Returns True if it's Friday after 15:00 UTC — no new signals."""
    n = now_utc()
    return n.weekday() == 4 and n.hour >= FRIDAY_WINDDOWN_HOUR


# ─────────────────────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────
def send_telegram(chat_id, text, reply_markup=None):
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def edit_telegram(chat_id, message_id, text, reply_markup=None):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def answer_callback(callback_query_id, text=None):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# SESSION & TIME
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
        return f"London opens in {(24 - h) + LONDON_OPEN}h"


def heartbeat_line():
    if LAST_SCAN_TIME:
        last = LAST_SCAN_TIME.strftime("%H:%M UTC")
        zones_count = len(ACTIVE_ZONES)
        zones_str = f"  ▪︎  Zones: {zones_count}" if zones_count else ""
        return f"☑ Online  ▪︎  Last scan: `{last}`{zones_str}"
    return "☑ Online  ▪︎  Awaiting first scan"


# ─────────────────────────────────────────────────────────────
# DATA LAYER
# ─────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, outputsize):
    """Fetch raw candles. Returns list of dicts (oldest first), or None."""
    if not TWELVE_DATA_KEY:
        log_error("fetch_candles", "TWELVE_DATA_KEY not set")
        return None
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
            log_error("fetch_candles", f"{symbol} {interval}: {res.get('message', 'API error')}")
            return None
        candles = res.get("values", [])
        if not candles:
            log_error("fetch_candles", f"{symbol} {interval}: empty response")
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
    except Exception as e:
        log_error("fetch_candles", f"{symbol} {interval}: {e}")
        return None


def resample_1h_to_2h(candles_1h):
    """Bucket 1H candles into 2H buckets aligned to even hours."""
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
    return [buckets[k] for k in sorted(buckets.keys())]


def get_2h_data(bars=30):
    global LAST_2H_SCAN_TIME
    needed = bars * 2 + 4
    raw = fetch_candles("XAU/USD", "1h", needed)
    if not raw or len(raw) < 10:
        return None
    res = resample_1h_to_2h(raw)
    if not res or len(res) < 10:
        return None
    LAST_2H_SCAN_TIME = now_utc()
    return res[-bars:]


def get_15m_data(bars=40):
    global LAST_15M_SCAN_TIME
    candles = fetch_candles("XAU/USD", "15min", bars)
    if candles:
        LAST_15M_SCAN_TIME = now_utc()
    return candles


def get_dxy_summary():
    """
    Returns dict: {trend: 'BULLISH'|'BEARISH'|'NEUTRAL'|'UNAVAILABLE',
                   price: float or None}
    Uses 1H DXY swings to determine direction.
    """
    global LAST_DXY_CHECK
    candles = fetch_candles("DXY", "1h", 24)
    if not candles or len(candles) < 15:
        # Try alternative symbol
        candles = fetch_candles("USDIDX", "1h", 24)
    if not candles or len(candles) < 15:
        result = {"trend": "UNAVAILABLE", "price": None}
        LAST_DXY_CHECK = {"time": now_utc().isoformat(), **result}
        return result

    atr_list = compute_atr(candles, period=14)
    swing_h, swing_l = detect_swings(candles, atr_list, lookback=3)

    if len(swing_h) < 2 or len(swing_l) < 2:
        result = {"trend": "NEUTRAL", "price": round(candles[-1]["close"], 2)}
        LAST_DXY_CHECK = {"time": now_utc().isoformat(), **result}
        return result

    last_h = swing_h[-1][1]
    prev_h = swing_h[-2][1]
    last_l = swing_l[-1][1]
    prev_l = swing_l[-2][1]

    if last_h > prev_h and last_l > prev_l:
        trend = "BULLISH"
    elif last_h < prev_h and last_l < prev_l:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    result = {"trend": trend, "price": round(candles[-1]["close"], 2)}
    LAST_DXY_CHECK = {"time": now_utc().isoformat(), **result}
    return result


# ─────────────────────────────────────────────────────────────
# INDICATORS (pure Python)
# ─────────────────────────────────────────────────────────────
def compute_atr(candles, period=ATR_PERIOD):
    if not candles or len(candles) < period + 1:
        return [None] * len(candles) if candles else []
    tr_list = [None]
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
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
    """Returns (highs, lows) as [(index, price), ...]."""
    swing_highs, swing_lows = [], []
    n = len(candles)
    for i in range(lookback, n - lookback):
        win_h = [candles[j]["high"] for j in range(i - lookback, i + lookback + 1)]
        win_l = [candles[j]["low"]  for j in range(i - lookback, i + lookback + 1)]
        local_avg = sum(c["close"] for c in candles[i - lookback:i + lookback + 1]) / (2 * lookback + 1)
        atr_here = atr_list[i] if atr_list[i] is not None else None

        if candles[i]["high"] == max(win_h):
            if atr_here is None or abs(candles[i]["high"] - local_avg) >= ATR_SIGNIFICANCE * atr_here:
                swing_highs.append((i, candles[i]["high"]))
        if candles[i]["low"] == min(win_l):
            if atr_here is None or abs(candles[i]["low"] - local_avg) >= ATR_SIGNIFICANCE * atr_here:
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
# STRUCTURE ANALYSIS
# ─────────────────────────────────────────────────────────────
def analyze_structure(candles, lookback=SWING_LOOKBACK):
    """
    Returns dict with structural read.
    Also includes BOS/CHoCH index for downstream OB/FVG detection.
    """
    if not candles or len(candles) < 15:
        return None

    atr_list = compute_atr(candles)
    swing_highs, swing_lows = detect_swings(candles, atr_list, lookback)
    consolidating = is_consolidating(candles, atr_list)

    current_price = candles[-1]["close"]
    atr_now = atr_list[-1]

    trend = "Ranging"
    bos = "None"
    bos_level = None
    bos_index = None
    bos_direction = None  # "bullish" | "bearish"
    choch = "None"
    last_high_val = last_low_val = prev_high_val = prev_low_val = None

    # Compute swing values if available — None if we don't have enough.
    if swing_highs:
        last_high_val = swing_highs[-1][1]
        if len(swing_highs) >= 2:
            prev_high_val = swing_highs[-2][1]
    if swing_lows:
        last_low_val = swing_lows[-1][1]
        if len(swing_lows) >= 2:
            prev_low_val = swing_lows[-2][1]

    # ── TREND CLASSIFICATION (requires 2 highs AND 2 lows) ──
    # This is informational. BOS detection below works WITHOUT it.
    if (len(swing_highs) >= 2 and len(swing_lows) >= 2):
        if last_high_val > prev_high_val and last_low_val > prev_low_val:
            trend = "Bullish"
        elif last_high_val < prev_high_val and last_low_val < prev_low_val:
            trend = "Bearish"
        # else: trend stays "Ranging"

    # ── BOS DETECTION (only needs 1 swing on the relevant side) ──
    # In SMC, BOS = price closes beyond the most recent significant swing.
    # We don't need a "confirmed trend" first — BOS is what STARTS a trend.
    if last_high_val is not None and current_price > last_high_val:
        bos = f"Bullish BOS @ `{round(last_high_val, 2)}`"
        bos_level = last_high_val
        bos_direction = "bullish"
        last_high_idx = swing_highs[-1][0]
        for i in range(last_high_idx + 1, len(candles)):
            if candles[i]["close"] > last_high_val:
                bos_index = i
                break
    elif last_low_val is not None and current_price < last_low_val:
        bos = f"Bearish BOS @ `{round(last_low_val, 2)}`"
        bos_level = last_low_val
        bos_direction = "bearish"
        last_low_idx = swing_lows[-1][0]
        for i in range(last_low_idx + 1, len(candles)):
            if candles[i]["close"] < last_low_val:
                bos_index = i
                break

    # ── CHoCH DETECTION ──
    # CHoCH = first counter-trend break, signaling potential reversal.
    # Needs an established directional trend AND 2 swings on each side.
    if (len(swing_highs) >= 2 and len(swing_lows) >= 2):
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
        "bos_level":       bos_level,
        "bos_index":       bos_index,
        "bos_direction":   bos_direction,
        "choch":           choch,
        "last_swing_high": round(last_high_val, 2) if last_high_val else None,
        "last_swing_low":  round(last_low_val, 2)  if last_low_val  else None,
        "consolidating":   consolidating,
        "atr":             round(atr_now, 2) if atr_now else None,
        "atr_raw":         atr_now,
        "candles":         candles,
    }


# ─────────────────────────────────────────────────────────────
# ORDER BLOCK DETECTION
# ─────────────────────────────────────────────────────────────
def detect_order_block(candles, bos_index, bos_direction, max_lookback=10):
    """
    Walk backward from the BOS-causing candle to find the Order Block.
    OB = the last candle whose direction OPPOSES the eventual breakout move.

    For a bullish BOS: OB = the last bearish candle before the bullish impulse
    For a bearish BOS: OB = the last bullish candle before the bearish impulse

    Returns dict {high, low, index} or None.
    """
    if bos_index is None or bos_index < 1:
        return None

    start = max(0, bos_index - max_lookback)
    # Walk backward from bos_index - 1
    for i in range(bos_index - 1, start - 1, -1):
        c = candles[i]
        is_bullish_candle = c["close"] > c["open"]
        is_bearish_candle = c["close"] < c["open"]

        if bos_direction == "bullish" and is_bearish_candle:
            # last bearish candle before bullish impulse
            return {"high": c["high"], "low": c["low"], "index": i}
        elif bos_direction == "bearish" and is_bullish_candle:
            # last bullish candle before bearish impulse
            return {"high": c["high"], "low": c["low"], "index": i}

    return None


# ─────────────────────────────────────────────────────────────
# FVG / IMBALANCE DETECTION
# ─────────────────────────────────────────────────────────────
def detect_fvg_in_impulse(candles, ob_index, bos_index, bos_direction):
    """
    Scan the impulse leg between OB and BOS for a 3-candle FVG.

    Bullish FVG: candle[i-1].high < candle[i+1].low  (gap zone is between them)
    Bearish FVG: candle[i-1].low  > candle[i+1].high

    Returns dict {high, low, index} or None.
    """
    if ob_index is None or bos_index is None or bos_index <= ob_index + 2:
        return None

    # Search window between OB and BOS
    for i in range(ob_index + 1, bos_index):
        if i < 1 or i >= len(candles) - 1:
            continue
        c_prev = candles[i - 1]
        c_next = candles[i + 1]

        if bos_direction == "bullish":
            if c_prev["high"] < c_next["low"]:
                # Bullish FVG: gap between c_prev.high and c_next.low
                return {
                    "high":  c_next["low"],
                    "low":   c_prev["high"],
                    "index": i,
                }
        else:  # bearish
            if c_prev["low"] > c_next["high"]:
                return {
                    "high":  c_prev["low"],
                    "low":   c_next["high"],
                    "index": i,
                }
    return None


# ─────────────────────────────────────────────────────────────
# ZONE LIFECYCLE
# ─────────────────────────────────────────────────────────────
def make_zone(direction, ob, fvg, bos_level):
    """Build a zone dict from OB and optional FVG."""
    # Combine OB and FVG into the trade zone.
    # If FVG exists, the zone is the union (OB + FVG envelope).
    if fvg:
        zone_high = max(ob["high"], fvg["high"])
        zone_low  = min(ob["low"],  fvg["low"])
    else:
        zone_high = ob["high"]
        zone_low  = ob["low"]

    zid = "z_" + now_utc().strftime("%Y%m%d_%H%M%S")
    return {
        "id":          zid,
        "direction":   direction,        # "bullish" or "bearish"
        "ob_high":     ob["high"],
        "ob_low":      ob["low"],
        "fvg_high":    fvg["high"] if fvg else None,
        "fvg_low":     fvg["low"]  if fvg else None,
        "zone_high":   zone_high,
        "zone_low":    zone_low,
        "bos_level":   bos_level,
        "created_at":  now_utc().isoformat(),
        "state":       "PENDING",
        "alerts_sent": [],  # list of state strings already alerted
    }


def is_zone_already_invalid(zone, candles_after_bos):
    """
    Per JP's rule: a freshly-detected zone is invalid if EITHER:
      1. Price has already tapped it (filled the zone), OR
      2. New structure has already formed beyond it (BOS in opposite direction
         after the original BOS — meaning original setup is no longer active)

    candles_after_bos: list of candles from BOS index onward.
    Returns True if zone should NOT be proposed.
    """
    if not candles_after_bos:
        return False

    zh, zl = zone["zone_high"], zone["zone_low"]
    direction = zone["direction"]

    # Check 1: has price already tapped the zone?
    for c in candles_after_bos:
        if c["low"] <= zh and c["high"] >= zl:
            return True  # already tapped -> invalid for proposal

    # Check 2: has new structure formed in opposite direction?
    # For bullish zone: opposite-direction structure = bearish move that
    # closes below the zone (a bearish BOS would be even stronger but
    # for a freshness check, "any close below zone" is sufficient).
    if direction == "bullish":
        for c in candles_after_bos:
            if c["close"] < zl:
                return True
    else:  # bearish zone
        for c in candles_after_bos:
            if c["close"] > zh:
                return True

    return False


def proximity_state(price, zone, atr):
    """
    Returns state: 'TAPPED' | 'NEAR' | 'APPROACHING' | 'FAR' | 'INVALIDATED'.
    Also handles invalidation (price closed beyond zone in wrong direction).
    """
    if atr is None or atr == 0:
        return "FAR"

    zh, zl = zone["zone_high"], zone["zone_low"]
    direction = zone["direction"]

    # Check tapped (price inside zone)
    if zl <= price <= zh:
        return "TAPPED"

    # Compute distance to nearest edge
    if direction == "bullish":
        # For bullish zone, we expect price coming DOWN to zone from above
        dist = price - zh  # positive if above zone
        if dist < 0:
            # Price is BELOW zone -> blew through it
            return "INVALIDATED"
    else:  # bearish
        # For bearish zone, we expect price coming UP from below
        dist = zl - price  # positive if below zone
        if dist < 0:
            # Price is ABOVE zone -> blew through
            return "INVALIDATED"

    if dist <= NEAR_ATR * atr:
        return "NEAR"
    elif dist <= APPROACH_ATR * atr:
        return "APPROACHING"
    return "FAR"


def add_active_zone(zone):
    """Add zone to active list, respecting the cap."""
    global ACTIVE_ZONES
    if len(ACTIVE_ZONES) >= MAX_ACTIVE_ZONES:
        # Remove the oldest one
        ACTIVE_ZONES.pop(0)
    ACTIVE_ZONES.append(zone)


def remove_zone(zone_id):
    global ACTIVE_ZONES
    ACTIVE_ZONES = [z for z in ACTIVE_ZONES if z["id"] != zone_id]


# ─────────────────────────────────────────────────────────────
# 15M TRIGGER DETECTION
# ─────────────────────────────────────────────────────────────
def check_15m_trigger(zone):
    """
    Check 15M for BOS or CHoCH that aligns with the zone direction.
    Returns dict {fired: bool, type: str, level: float} or {fired: False, ...}.
    """
    candles_15m = get_15m_data(bars=40)
    if not candles_15m or len(candles_15m) < 15:
        return {"fired": False, "reason": "no 15M data"}

    structure = analyze_structure(candles_15m, lookback=LTF_SWING_LOOKBACK)
    if not structure:
        return {"fired": False, "reason": "15M structure unclear"}

    direction = zone["direction"]

    # Bullish zone needs bullish 15M trigger (BOS up or CHoCH up)
    if direction == "bullish":
        if structure["bos_direction"] == "bullish":
            return {"fired": True, "type": "15M Bullish BOS", "level": structure["bos_level"]}
        if "Bullish CHoCH" in structure["choch"]:
            return {"fired": True, "type": "15M Bullish CHoCH",
                    "level": structure["last_swing_high"]}
    else:  # bearish zone
        if structure["bos_direction"] == "bearish":
            return {"fired": True, "type": "15M Bearish BOS", "level": structure["bos_level"]}
        if "Bearish CHoCH" in structure["choch"]:
            return {"fired": True, "type": "15M Bearish CHoCH",
                    "level": structure["last_swing_low"]}

    return {"fired": False, "reason": "no 15M trigger yet"}


# ─────────────────────────────────────────────────────────────
# TRADE LEVEL CALCULATION (matches your chart model)
# ─────────────────────────────────────────────────────────────
def calculate_trade_levels(zone, current_price):
    """
    Entry at OB zone, SL beyond OB with buffer, TP at exactly 3R.
    Matches the JP entry model: entry=OB, SL=just beyond OB, TP=3R.
    """
    direction = zone["direction"]
    ob_high = zone["ob_high"]
    ob_low = zone["ob_low"]
    zone_height = ob_high - ob_low
    buffer = max(zone_height * 0.15, 0.5)  # small buffer beyond OB

    if direction == "bullish":
        entry = (ob_high + ob_low) / 2  # mid-zone for limit order
        sl    = ob_low - buffer
        risk  = entry - sl
        if risk <= 0:
            return None
        tp = entry + (risk * MIN_RR)
    else:  # bearish
        entry = (ob_high + ob_low) / 2
        sl    = ob_high + buffer
        risk  = sl - entry
        if risk <= 0:
            return None
        tp = entry - (risk * MIN_RR)

    return {
        "entry":      round(entry, 2),
        "sl":         round(sl, 2),
        "tp":         round(tp, 2),
        "risk":       round(risk, 2),
        "rr":         MIN_RR,
        "direction":  direction,
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
                {"text": "▫️ Active Zones",   "callback_data": "active_zones"},
                {"text": "▫️ Bot Status",      "callback_data": "status"},
            ],
            [
                {"text": "▫️ Entry Rules",    "callback_data": "rules"},
                {"text": "▫️ A+ Checklist",   "callback_data": "checklist"},
            ],
            [
                {"text": "▫️ Session",         "callback_data": "session_info"},
                {"text": "▫️ Trade Log",       "callback_data": "trade_log"},
            ],
        ]
    }


def zone_validation_buttons(zone_id):
    return {
        "inline_keyboard": [
            [
                {"text": "✓ Valid", "callback_data": f"valid_{zone_id}"},
                {"text": "✗ Poor",  "callback_data": f"poor_{zone_id}"},
            ],
            [{"text": "✏️ Edit", "callback_data": f"edit_{zone_id}"}],
            [{"text": "▪︎ Back to Dashboard", "callback_data": "dashboard"}],
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
   ▪︎  Mark Order Block (last opposite candle before impulse)
   ▪︎  Confirm zone validity manually (Valid / Poor)

*3.  ENTRY TRIGGER (15M)*
   ▪︎  Wait for price to approach or tap marked zone
   ▪︎  Look for 15M BOS or CHoCH inside or near zone
   ▪︎  Entry only on confirmation

*4.  EXECUTION*
   ▪︎  Entry at OB mid-zone
   ▪︎  SL just beyond OB (with small buffer)
   ▪︎  TP at exactly 3R from entry

*5.  CONFLUENCE*
   ▪︎  DXY moving opposite to trade direction
   ▪︎  Active session: London or New York

*6.  RISK*
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
▫️  *Signals:*        `#signal`
▫️  *London only:*    `#london`
▫️  *NY only:*        `#newyork`

{DIVIDER}
_Every signal and zone is auto-logged._
"""


def status_message():
    """Detailed bot status for Monday troubleshooting."""
    def fmt_time(t):
        if not t:
            return "_never_"
        if isinstance(t, str):
            return f"`{t[11:16]} UTC` ({t[:10]})"
        return f"`{t.strftime('%H:%M UTC')}` ({t.strftime('%Y-%m-%d')})"

    last_scan = fmt_time(LAST_SCAN_TIME)
    last_2h   = fmt_time(LAST_2H_SCAN_TIME)
    last_15m  = fmt_time(LAST_15M_SCAN_TIME)

    dxy_line = "_no DXY check yet_"
    if LAST_DXY_CHECK:
        dxy_line = f"{LAST_DXY_CHECK['trend']} ▪︎ price `{LAST_DXY_CHECK.get('price', 'n/a')}` ▪︎ {fmt_time(LAST_DXY_CHECK['time'])}"

    error_line = "_none_"
    if LAST_ERROR:
        error_line = f"`{LAST_ERROR['where']}` ▪︎ {fmt_time(LAST_ERROR['time'])}\n_{LAST_ERROR['message']}_"

    zones_line = f"`{len(ACTIVE_ZONES)}` active ▪︎ `{len(PENDING_ZONES)}` pending"

    proposed_line = f"`{len(PROPOSED_BOS_LEVELS)}` BOS levels seen this session"

    winddown_line = "🔔 *Friday wind-down ACTIVE*" if is_friday_winddown() else ""

    return f"""
☑  *BOT STATUS*  ▪︎  v2.0
{DIVIDER}
*Session:*       {get_session_label()}
*Active:*        {"Yes" if is_session_active() else "No"}
{winddown_line}
{DIVIDER}
*Last scan:*     {last_scan}
*Last 2H pull:*  {last_2h}
*Last 15M pull:* {last_15m}
*DXY:*           {dxy_line}
{DIVIDER}
*Zones:*         {zones_line}
*Memory:*        {proposed_line}
{DIVIDER}
*Today's counts:*
  Alerts sent:   `{ALERTS_SENT_TODAY}`
  Signals fired: `{SIGNALS_SENT_TODAY}`
{DIVIDER}
*Last error:*
{error_line}
{DIVIDER}
"""


def config_message():
    """Show current strategy parameters (read-only)."""
    return f"""
⚙️  *BOT CONFIG*  (read-only)
{DIVIDER}
*Strategy:*
  Swing lookback:        `{SWING_LOOKBACK}` bars
  ATR period:            `{ATR_PERIOD}`
  ATR significance:      `{ATR_SIGNIFICANCE}` × ATR
  Consolidation ratio:   `{CONSOLIDATION_RATIO}` × ATR
  Min RR:                `{MIN_RR}`
  Risk per trade:        `{RISK_PERCENT}%`

*Proximity:*
  Approaching threshold: `{APPROACH_ATR}` × ATR
  Near threshold:        `{NEAR_ATR}` × ATR

*15M trigger:*
  LTF swing lookback:    `{LTF_SWING_LOOKBACK}`

*Sessions (UTC):*
  London:                `{LONDON_OPEN}:00 – {LONDON_CLOSE}:00`
  New York:              `{NY_OPEN}:00 – {NY_CLOSE}:00`
  Friday wind-down:      `{FRIDAY_WINDDOWN_HOUR}:00`

*Caps:*
  Max active zones:      `{MAX_ACTIVE_ZONES}`
{DIVIDER}
_To change parameters, edit app.py and redeploy._
"""


def active_zones_message():
    if not ACTIVE_ZONES:
        return f"""
*ACTIVE ZONES*
{DIVIDER}
_No active zones right now._
The bot will mark new zones when it detects fresh 2H BOS during sessions.
"""
    lines = [f"*ACTIVE ZONES*  ({len(ACTIVE_ZONES)})", DIVIDER]
    for z in ACTIVE_ZONES:
        arrow = "▲" if z["direction"] == "bullish" else "▼"
        fvg_str = f" + FVG `{round(z['fvg_low'],2)}-{round(z['fvg_high'],2)}`" if z["fvg_high"] else ""
        lines.append(f"{arrow}  *{z['direction'].upper()}* zone  ▪︎  `{round(z['zone_low'],2)} – {round(z['zone_high'],2)}`")
        lines.append(f"    State: `{z['state']}`{fvg_str}")
        lines.append(f"    Created: `{z['created_at'][11:16]} UTC`")
    lines.append(DIVIDER)
    return "\n".join(lines)


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


def format_zone_proposal(zone):
    """The message that asks user to validate a freshly marked zone."""
    direction = zone["direction"]
    arrow = "▲" if direction == "bullish" else "▼"
    fvg_str = ""
    if zone["fvg_high"] is not None:
        fvg_str = f"\nFVG:        `{round(zone['fvg_low'],2)} – {round(zone['fvg_high'],2)}`"

    return f"""
🔔  *NEW ZONE DETECTED — GOLD*
{DIVIDER}
Direction:  {arrow}  *{direction.upper()}*
OB:         `{round(zone['ob_low'],2)} – {round(zone['ob_high'],2)}`{fvg_str}
Total zone: `{round(zone['zone_low'],2)} – {round(zone['zone_high'],2)}`
2H BOS at:  `{round(zone['bos_level'],2)}`

{DIVIDER}
Verify on chart and confirm:
"""


def format_proximity_alert(zone, state, current_price, dxy):
    arrow = "▲" if zone["direction"] == "bullish" else "▼"
    state_emoji = {
        "APPROACHING": "⚪",
        "NEAR":        "🟡",
        "TAPPED":      "🟧",
    }.get(state, "▫️")

    dxy_line = ""
    if dxy and dxy["trend"] != "UNAVAILABLE":
        dxy_aligned = (
            (zone["direction"] == "bullish" and dxy["trend"] == "BEARISH") or
            (zone["direction"] == "bearish" and dxy["trend"] == "BULLISH")
        )
        check = "✓" if dxy_aligned else "✗"
        dxy_line = f"\nDXY:        {dxy['trend']}  {check}"

    return f"""
{state_emoji}  *ZONE {state} — GOLD*
{DIVIDER}
Direction:  {arrow}  {zone['direction'].upper()}
Zone:       `{round(zone['zone_low'],2)} – {round(zone['zone_high'],2)}`
Price:      `{round(current_price,2)}`{dxy_line}

_Watching 15M for BOS/CHoCH..._
{DIVIDER}
"""


def format_signal(zone, levels, trigger, dxy, current_price):
    arrow = "▲" if zone["direction"] == "bullish" else "▼"
    side = "🟢 BUY" if zone["direction"] == "bullish" else "🔴 SELL"

    dxy_line = "DXY:        UNAVAILABLE"
    dxy_aligned = False
    if dxy and dxy["trend"] != "UNAVAILABLE":
        dxy_aligned = (
            (zone["direction"] == "bullish" and dxy["trend"] == "BEARISH") or
            (zone["direction"] == "bearish" and dxy["trend"] == "BULLISH")
        )
        check = "✓" if dxy_aligned else "✗ ⚠"
        dxy_line = f"DXY:        {dxy['trend']}  {check}"

    return f"""
🔔  *A+ SIGNAL FIRED — GOLD*
{DIVIDER}
{side}  {arrow}  *{zone['direction'].upper()}*

Entry:      `{levels['entry']}`
SL:         `{levels['sl']}`
TP:         `{levels['tp']}`
Risk:       `{levels['risk']}` pts
RR:         `1:{levels['rr']}`
{DIVIDER}
*Trigger:* {trigger['type']}
*Zone:* `{round(zone['zone_low'],2)} – {round(zone['zone_high'],2)}`
*Price now:* `{round(current_price,2)}`
{dxy_line}
Session:    {get_session_label()}
{DIVIDER}
⚠ _Always confirm on TradingView before execution._
_Risk 0.5% — verify spread and slippage._

#signal #gold #{zone['direction']} #log
"""


def format_zone_log(zone, status):
    """Permanent Telegram log entry for a zone (Valid / Closed)."""
    arrow = "▲" if zone["direction"] == "bullish" else "▼"
    fvg_str = ""
    if zone["fvg_high"] is not None:
        fvg_str = f"\nFVG: `{round(zone['fvg_low'],2)}-{round(zone['fvg_high'],2)}`"
    return f"""
🎯  *ZONE {status.upper()} — GOLD*
{DIVIDER}
{arrow}  {zone['direction'].upper()}
OB: `{round(zone['ob_low'],2)} – {round(zone['ob_high'],2)}`{fvg_str}
Created: `{zone['created_at'][:16]} UTC`

#zone #gold #{zone['direction']} #{status.lower()} #log
"""


# ─────────────────────────────────────────────────────────────
# CORE ANALYSIS FLOW (manual trigger)
# ─────────────────────────────────────────────────────────────
def run_gold_analysis():
    """Manual analysis triggered by Force Scan or analyze_gold button."""
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
            "body": "Could not fetch gold 2H data. Try again in a moment.",
        }

    structure = analyze_structure(candles_2h)
    if structure is None:
        return False, {
            "headline": "⚠  *Structure Analysis Failed*",
            "body": "Insufficient data on 2H. Try again later.",
        }

    LAST_SCAN_TIME = now_utc()
    card = format_structure_card(structure)
    return True, {"headline": None, "body": card, "structure": structure}


# ─────────────────────────────────────────────────────────────
# AUTO-SCAN — Proactive Engine
# ─────────────────────────────────────────────────────────────
def auto_market_scan():
    """
    Runs on each UptimeRobot ping (~5 min).
    The proactive brain of the bot.
    Hardened in Session 4 with: error handling, BOS dedup,
    zone freshness checks, Friday wind-down, observability.
    """
    global LAST_SCAN_TIME, LAST_2H_BOS_LEVEL, LAST_2H_BOS_DIRECTION
    global LAST_SESSION_OPEN_NOTIFIED, LAST_FRIDAY_WINDDOWN
    global ALERTS_SENT_TODAY, SIGNALS_SENT_TODAY

    if not is_session_active():
        return
    if not CHAT_ID:
        return

    reset_daily_counters_if_needed()

    try:
        n = now_utc()
        today = n.strftime("%Y-%m-%d")

        # ── Step 1: Session-open notification (once per session per day)
        session_key = None
        if n.hour == LONDON_OPEN and n.minute < 10:
            session_key = f"{today}-london"
        elif n.hour == NY_OPEN and n.minute < 10:
            session_key = f"{today}-newyork"

        if session_key and session_key != LAST_SESSION_OPEN_NOTIFIED:
            LAST_SESSION_OPEN_NOTIFIED = session_key
            label = "London" if "london" in session_key else "New York"
            send_telegram(
                CHAT_ID,
                f"🔔  *{label} session open*\n{DIVIDER}\nGold bot is active — watching for setups.\n#log #{label.lower().replace(' ','')}",
            )
            ALERTS_SENT_TODAY += 1

        # ── Step 1b: Friday wind-down (once per Friday)
        friday_key = today if is_friday_winddown() else None
        if friday_key and friday_key != LAST_FRIDAY_WINDDOWN:
            LAST_FRIDAY_WINDDOWN = friday_key
            # Clear all active zones
            cleared = len(ACTIVE_ZONES)
            ACTIVE_ZONES.clear()
            PROPOSED_BOS_LEVELS.clear()
            send_telegram(
                CHAT_ID,
                f"🔔  *Friday wind-down*\n{DIVIDER}\nNo new signals after 15:00 UTC Friday.\nCleared `{cleared}` active zone(s).\nMarkets resume Monday London open.\n#log #friday",
            )
            ALERTS_SENT_TODAY += 1
            return  # nothing else to do after winddown

        # ── Step 2: Pull 2H structure
        candles_2h = get_2h_data(bars=30)
        if not candles_2h:
            log_error("auto_scan", "no 2H data")
            return
        structure = analyze_structure(candles_2h)
        if not structure:
            log_error("auto_scan", "no 2H structure")
            return

        LAST_SCAN_TIME = now_utc()
        current_price = structure["current_price"]
        atr = structure["atr_raw"]

        # ── Step 3: Skip everything if consolidating
        if structure["consolidating"]:
            return

        # ── Step 4: Direction-flip detection
        # If 2H direction has flipped (bullish trend turned bearish or vice
        # versa), reset the proposed BOS memory so we can propose new zones.
        if (structure["bos_direction"] and
                LAST_2H_BOS_DIRECTION and
                structure["bos_direction"] != LAST_2H_BOS_DIRECTION):
            PROPOSED_BOS_LEVELS.clear()

        # ── Step 5: Detect fresh 2H BOS we haven't proposed yet
        if (not is_friday_winddown() and
                structure["bos_level"] is not None and
                structure["bos_index"] is not None):

            bos_key = (structure["bos_direction"], round(structure["bos_level"], 2))

            if bos_key not in PROPOSED_BOS_LEVELS:
                # Fresh BOS — find OB and FVG
                ob = detect_order_block(
                    structure["candles"],
                    structure["bos_index"],
                    structure["bos_direction"],
                )
                if ob:
                    fvg = detect_fvg_in_impulse(
                        structure["candles"],
                        ob["index"],
                        structure["bos_index"],
                        structure["bos_direction"],
                    )
                    zone = make_zone(
                        structure["bos_direction"],
                        ob,
                        fvg,
                        structure["bos_level"],
                    )

                    # Freshness check (JP's rule):
                    # Has price already tapped the zone, OR has new structure
                    # formed beyond it? If yes -> setup is dead, skip it.
                    candles_after_bos = structure["candles"][structure["bos_index"] + 1:]
                    if is_zone_already_invalid(zone, candles_after_bos):
                        # Mark as proposed so we don't re-check it endlessly
                        PROPOSED_BOS_LEVELS.add(bos_key)
                    else:
                        PENDING_ZONES[zone["id"]] = zone
                        PROPOSED_BOS_LEVELS.add(bos_key)
                        LAST_2H_BOS_LEVEL = structure["bos_level"]
                        LAST_2H_BOS_DIRECTION = structure["bos_direction"]

                        # Send zone proposal to user
                        send_telegram(
                            CHAT_ID,
                            format_zone_proposal(zone),
                            zone_validation_buttons(zone["id"]),
                        )
                        ALERTS_SENT_TODAY += 1

        # ── Step 6: Track active zones (proximity + 15M trigger)
        dxy = None  # fetch lazily only when needed

        for zone in list(ACTIVE_ZONES):
            try:
                state = proximity_state(current_price, zone, atr)
            except Exception as e:
                log_error("proximity_state", str(e))
                continue

            # Handle invalidation
            if state == "INVALIDATED":
                send_telegram(
                    CHAT_ID,
                    f"✗  *Zone invalidated*\n{DIVIDER}\n{zone['direction'].upper()} zone `{round(zone['zone_low'],2)}-{round(zone['zone_high'],2)}` — price closed beyond it without trigger.\n\n#zone #gold #closed #invalidated #log",
                )
                remove_zone(zone["id"])
                ALERTS_SENT_TODAY += 1
                continue

            # Send proximity alerts (each state once per zone)
            if state in ("APPROACHING", "NEAR", "TAPPED") and state not in zone["alerts_sent"]:
                zone["alerts_sent"].append(state)
                zone["state"] = state

                if dxy is None:
                    dxy = get_dxy_summary()

                send_telegram(CHAT_ID, format_proximity_alert(zone, state, current_price, dxy))
                ALERTS_SENT_TODAY += 1

            # Check 15M trigger when zone is at least APPROACHING.
            # Skip during Friday wind-down.
            if state in ("APPROACHING", "NEAR", "TAPPED") and not is_friday_winddown():
                # Don't fire same trigger twice (track via zone state)
                if "SIGNAL_FIRED" in zone["alerts_sent"]:
                    continue

                try:
                    trigger = check_15m_trigger(zone)
                except Exception as e:
                    log_error("check_15m_trigger", str(e))
                    continue

                if trigger.get("fired"):
                    levels = calculate_trade_levels(zone, current_price)
                    if levels:
                        if dxy is None:
                            dxy = get_dxy_summary()
                        send_telegram(CHAT_ID, format_signal(zone, levels, trigger, dxy, current_price))
                        send_telegram(CHAT_ID, format_zone_log(zone, "fired"))
                        zone["alerts_sent"].append("SIGNAL_FIRED")
                        SIGNALS_SENT_TODAY += 1
                        ALERTS_SENT_TODAY += 1
                        remove_zone(zone["id"])

    except Exception as e:
        log_error("auto_market_scan", str(e))


# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────
@app.route("/test_functions")
def test_functions():
    """
    Smoke test endpoint — verifies all critical functions exist and are callable.
    Hit this after every deploy. Healthy output means the build didn't lose
    any function definitions during patching.
    """
    critical_funcs = [
        "fetch_candles", "resample_1h_to_2h", "get_2h_data", "get_15m_data",
        "get_dxy_summary", "compute_atr", "detect_swings", "is_consolidating",
        "analyze_structure", "detect_order_block", "detect_fvg_in_impulse",
        "make_zone", "is_zone_already_invalid", "proximity_state",
        "add_active_zone", "remove_zone", "check_15m_trigger",
        "calculate_trade_levels", "run_gold_analysis", "auto_market_scan",
    ]
    import sys
    this_module = sys.modules[__name__]
    results = {}
    for name in critical_funcs:
        f = getattr(this_module, name, None)
        results[name] = "OK" if callable(f) else "MISSING"
    all_ok = all(v == "OK" for v in results.values())
    return {
        "all_functions_ok": all_ok,
        "missing": [k for k, v in results.items() if v == "MISSING"],
        "checked": len(critical_funcs),
        "version": "2.0-s4-fixed",
    }, 200 if all_ok else 500


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
        "friday_winddown": is_friday_winddown(),
        "last_scan": LAST_SCAN_TIME.isoformat() if LAST_SCAN_TIME else None,
        "last_2h_scan": LAST_2H_SCAN_TIME.isoformat() if LAST_2H_SCAN_TIME else None,
        "last_15m_scan": LAST_15M_SCAN_TIME.isoformat() if LAST_15M_SCAN_TIME else None,
        "active_zones": len(ACTIVE_ZONES),
        "pending_zones": len(PENDING_ZONES),
        "proposed_bos_levels": len(PROPOSED_BOS_LEVELS),
        "alerts_today": ALERTS_SENT_TODAY,
        "signals_today": SIGNALS_SENT_TODAY,
        "last_error": LAST_ERROR,
        "version": "2.0-s4-fixed",
    }, 200


@app.route("/startup")
def startup():
    if CHAT_ID:
        send_telegram(CHAT_ID, dashboard_message(), main_menu())
    return "ok", 200


# ── Webhook ────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return "ok", 200

    # ─── Inline button presses ──────────────────────────────
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        message_id = cb["message"]["message_id"]
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

        elif action == "active_zones":
            send_telegram(chat_id, active_zones_message(), back_button())

        elif action == "status":
            send_telegram(chat_id, status_message(), back_button())

        elif action in ("analyze_gold", "force_scan"):
            label = "*Analyzing gold...*" if action == "analyze_gold" else "*Force scanning gold...*"
            send_telegram(chat_id, label)
            ok, result = run_gold_analysis()
            if ok:
                send_telegram(chat_id, result["body"], back_button())
            else:
                msg = f"{result['headline']}\n{DIVIDER}\n{result['body']}"
                send_telegram(chat_id, msg, back_button())

        # ── Zone validation buttons ──
        elif action.startswith("valid_"):
            zid = action[len("valid_"):]
            zone = PENDING_ZONES.pop(zid, None)
            if zone:
                zone["state"] = "VALIDATED"
                add_active_zone(zone)
                edit_telegram(
                    chat_id, message_id,
                    f"✓  *Zone validated*  ▪︎  watching\n{DIVIDER}\n{zone['direction'].upper()} `{round(zone['zone_low'],2)}-{round(zone['zone_high'],2)}`",
                )
                send_telegram(chat_id, format_zone_log(zone, "active"))
            else:
                send_telegram(chat_id, "_Zone not found (may have expired)._", back_button())

        elif action.startswith("poor_"):
            zid = action[len("poor_"):]
            zone = PENDING_ZONES.pop(zid, None)
            if zone:
                edit_telegram(
                    chat_id, message_id,
                    f"✗  *Zone discarded*\n{DIVIDER}\n_Bot will not track this one._",
                )
            else:
                send_telegram(chat_id, "_Zone not found._", back_button())

        elif action.startswith("edit_"):
            zid = action[len("edit_"):]
            zone = PENDING_ZONES.get(zid)
            if zone:
                EDIT_PROMPTS[chat_id] = zid
                send_telegram(
                    chat_id,
                    f"✏️  *Edit zone*\n{DIVIDER}\nReply with corrected zone as:\n`high, low`\n\nExample: `4605.00, 4598.00`\n\n_Current: {round(zone['zone_high'],2)}, {round(zone['zone_low'],2)}_",
                )

    # ─── Text/photo messages ──────────────────────────────
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")

        # Check if user is responding to an edit prompt
        if chat_id in EDIT_PROMPTS:
            zid = EDIT_PROMPTS[chat_id]
            zone = PENDING_ZONES.get(zid)
            if zone:
                try:
                    parts = [p.strip() for p in text.replace(" ", "").split(",")]
                    high = float(parts[0])
                    low = float(parts[1])
                    if high < low:
                        high, low = low, high
                    zone["zone_high"] = high
                    zone["zone_low"]  = low
                    zone["ob_high"]   = high
                    zone["ob_low"]    = low
                    zone["fvg_high"]  = None
                    zone["fvg_low"]   = None
                    zone["state"]     = "VALIDATED"
                    PENDING_ZONES.pop(zid, None)
                    add_active_zone(zone)
                    EDIT_PROMPTS.pop(chat_id, None)
                    send_telegram(
                        chat_id,
                        f"✓  *Zone updated and validated*\n{DIVIDER}\n{zone['direction'].upper()} `{round(low,2)}-{round(high,2)}` — watching",
                        back_button(),
                    )
                    send_telegram(chat_id, format_zone_log(zone, "active"))
                except (ValueError, IndexError):
                    send_telegram(
                        chat_id,
                        f"_Invalid format. Send as `high, low` — e.g. `4605.00, 4598.00`_",
                    )
            return "ok", 200

        # Regular commands
        if text in ["/start", "/menu", "/dashboard"]:
            send_telegram(chat_id, dashboard_message(), main_menu())
        elif text == "/scan":
            send_telegram(chat_id, "*Force scanning gold...*")
            ok, result = run_gold_analysis()
            if ok:
                send_telegram(chat_id, result["body"], back_button())
            else:
                send_telegram(chat_id, f"{result['headline']}\n{DIVIDER}\n{result['body']}", back_button())
        elif text == "/zones":
            send_telegram(chat_id, active_zones_message(), back_button())
        elif text == "/status":
            send_telegram(chat_id, status_message(), back_button())
        elif text == "/config":
            send_telegram(chat_id, config_message(), back_button())
        elif text == "/health":
            send_telegram(
                chat_id,
                f"☑ *Bot Health*\n{DIVIDER}\nVersion: `2.0-s4-fixed`\nSession: {get_session_label()}\nActive zones: `{len(ACTIVE_ZONES)}`\n{heartbeat_line()}\n\n_For details, use_ `/status`",
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
