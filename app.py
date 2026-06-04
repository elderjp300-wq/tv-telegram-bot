"""
=============================================================================
JP GOLD BOT v3.2 — Setup Detector (automation-ready records)
=============================================================================
This bot is a SETUP DETECTOR, not a trader. It detects clean textbook 15M SMC
setups, draws them, logs them, and shows them. The human validates each against
TradingView. We are training the detector's eye, not trading its output.

Strategy (locked):
  - 2H trend = CONTEXT ONLY (recorded, never filters)
  - 15M detection: BOS on latest closed candle -> Order Block -> MANDATORY FVG
  - Entry = OB open. Stop = OB invalidation (+buffer). Target = 3R fixed.
  - NO 2H swing targets. NO grading filter. NO trade tracking. NO add-on logic.
  - One signal per unique order block (deduped). Human filters by eye.

Records are AUTOMATION-READY: every signal carries the full field set, with
MetaApi/outcome fields null until automation is live. A /signals endpoint
exposes them (with CORS) for the JP Signals viewer app.

Operations:
  - XAUUSDm (configurable via MT_SYMBOL)
  - Scans once per closed 15M candle
  - London + NY sessions fire by default; Asia optional (INCLUDE_ASIA)
  - Session tagged on every signal regardless, for win-rate-by-session later
=============================================================================
"""

import os
import io
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import requests
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# =============================================================================
# CONFIG
# =============================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "")
PORT = int(os.getenv("PORT", "10000"))

INSTRUMENT_DISPLAY = os.getenv("MT_SYMBOL", "XAUUSDm")
INSTRUMENT_API = "XAU/USD"

# Sessions (UTC). London+NY fire by default; Asia optional.
SESSION_START_UTC = 7
SESSION_END_UTC = 22
INCLUDE_ASIA = os.getenv("INCLUDE_ASIA", "false").lower() == "true"
ASIA_START_UTC = 0
ASIA_END_UTC = 7

# Position-sizing intent for automation (percent of account risked per trade).
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))  # 0.1 - 1.0

# Timeframes
TF_2H = "2h"
TF_15M = "15min"
CANDLES_2H = 80
CANDLES_15M = 60

# Strategy params
SWING_LOOKBACK = 3
RR_TARGET = 3.0
ATR_PERIOD = 14
# --- Setup-quality rules ---
# OB candle range gate is a FIXED DOLLAR window on (high-low), per JP: $2..$22.
# FVG min size and the stop buffer stay ATR-relative (self-adjust to volatility).
# All overridable via env if you tune them later.
OB_MIN_USD         = float(os.getenv("OB_MIN_USD", "2.0"))
OB_MAX_USD         = float(os.getenv("OB_MAX_USD", "22.0"))
FVG_MIN_ATR_MULT   = float(os.getenv("FVG_MIN_ATR_MULT", "0.2"))
SL_BUFFER_ATR_MULT = float(os.getenv("SL_BUFFER_ATR_MULT", "0.2"))
ORDER_EXPIRY_HOURS = 24   # placed limit must tap within a day (automation rule)

SCAN_INTERVAL_SECONDS = 15 * 60
MAX_LOGGED_SIGNALS = 200

# Runtime flags (paused, last scan, etc.) live in this small file — losing them
# on a restart is harmless. SIGNALS live in Supabase (durable, survive restarts).
STATE_FILE = "bot_state.json"

# Supabase (durable signal storage). Set in Render env vars.
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
# Shared secret the execution worker presents to write broker-truth outcome
# fields back onto a signal. The public app never needs this (it only writes
# grade/notes/eye_agreement). If unset, broker write-back is disabled.
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")
SUPABASE_TABLE = "signals"
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    level=logging.INFO)
log = logging.getLogger("jp-gold-bot")
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# =============================================================================
# STATE
# =============================================================================
state: Dict[str, Any] = {
    "paused": False,
    "last_scan_ts": None,
    "last_2h_trend": None,
    "last_signal_ob_key": None,
    "signals": [],
    "signals_today": 0,
    "today_date": None,
    "bot_started_at": datetime.now(timezone.utc).isoformat(),
}


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


# Public Supabase Storage bucket that holds setup chart PNGs (so the web app
# can display them). Created once in the Supabase dashboard; see deploy notes.
SUPABASE_CHART_BUCKET = os.getenv("SUPABASE_CHART_BUCKET", "charts")


def sb_upload_chart(signal_id: str, png_bytes: bytes) -> Optional[str]:
    """Upload a chart PNG to the public Storage bucket and return its public
    URL (or None on failure). Used to fill chart_url so the app can show it."""
    if not SUPABASE_ENABLED or not png_bytes:
        return None
    fname = f"{signal_id}.png"
    try:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_CHART_BUCKET}/{fname}",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "image/png",
                "x-upsert": "true",   # overwrite if it already exists
            },
            data=png_bytes, timeout=20)
        if r.ok:
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_CHART_BUCKET}/{fname}"
        log.error(f"sb_upload_chart: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"sb_upload_chart: {e}")
    return None


def sb_load_signals():
    """Load all signals from Supabase, newest first. Returns list (or [] on error)."""
    if not SUPABASE_ENABLED:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
            headers=_sb_headers(),
            params={"select": "*", "order": "timestamp.desc", "limit": str(MAX_LOGGED_SIGNALS)},
            timeout=15)
        if r.ok:
            return r.json()
        log.error(f"sb_load_signals: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"sb_load_signals: {e}")
    return []


def sb_upsert_signal(record: Dict[str, Any]) -> bool:
    """Insert or update one signal row by id (Supabase upsert via Prefer header)."""
    if not SUPABASE_ENABLED:
        return False
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
            json=record, timeout=15)
        if r.ok:
            return True
        log.error(f"sb_upsert_signal: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"sb_upsert_signal: {e}")
    return False


def load_state():
    """Load runtime flags from the JSON file (ephemeral, fine to lose) and the
    durable signals list from Supabase (survives restarts)."""
    global state
    # 1. runtime flags from file
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                flags = json.load(f)
            # only adopt the small runtime flags, never an old signals list
            for k in ("paused", "last_scan_ts", "last_2h_trend",
                      "last_signal_ob_key", "signals_today", "today_date"):
                if k in flags:
                    state[k] = flags[k]
            log.info("Runtime flags loaded from file")
        except Exception as e:
            log.warning(f"Could not load flags: {e}")
    # 2. durable signals from Supabase
    if SUPABASE_ENABLED:
        sigs = sb_load_signals()
        state["signals"] = sigs
        log.info(f"Loaded {len(sigs)} signals from Supabase")
    else:
        log.warning("Supabase not configured — signals will not persist!")


def save_flags():
    """Persist only the small runtime flags to the JSON file (NOT signals)."""
    try:
        flags = {k: state.get(k) for k in
                 ("paused", "last_scan_ts", "last_2h_trend",
                  "last_signal_ob_key", "signals_today", "today_date")}
        with open(STATE_FILE, "w") as f:
            json.dump(flags, f, indent=2, default=str)
    except Exception as e:
        log.error(f"save_flags: {e}")


def save_state():
    """Backward-compatible: persists runtime flags. Signals are saved per-row
    via sb_upsert_signal at the moment they're created/updated, so this no
    longer needs to write the whole signals list."""
    save_flags()


def reset_daily_counter():
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("today_date") != today:
        state["today_date"] = today
        state["signals_today"] = 0


# =============================================================================
# DATA
# =============================================================================
def fetch_candles(timeframe: str, count: int) -> Optional[List[Dict]]:
    if not TWELVE_DATA_KEY:
        log.error("TWELVE_DATA_KEY not set")
        return None
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": INSTRUMENT_API, "interval": timeframe,
            "outputsize": count, "apikey": TWELVE_DATA_KEY, "format": "JSON",
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            log.error(f"Twelve Data error: {data.get('message')}")
            return None
        values = data.get("values", [])
        if not values:
            return None
        candles = [{
            "time": v["datetime"], "open": float(v["open"]),
            "high": float(v["high"]), "low": float(v["low"]),
            "close": float(v["close"]),
        } for v in reversed(values)]
        # Drop the forming candle — only closed bars downstream.
        if len(candles) > 1:
            candles = candles[:-1]
        return candles
    except Exception as e:
        log.error(f"fetch_candles({timeframe}): {e}")
        return None


# =============================================================================
# STRUCTURE DETECTION (deterministic candle math)
# =============================================================================
def detect_swings(candles: List[Dict], lookback: int = SWING_LOOKBACK) -> List[Dict]:
    swings = []
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        if all(candles[i + j]["high"] <= c["high"]
               for j in range(-lookback, lookback + 1) if j != 0):
            swings.append({"idx": i, "type": "high", "price": c["high"], "time": c["time"]})
        if all(candles[i + j]["low"] >= c["low"]
               for j in range(-lookback, lookback + 1) if j != 0):
            swings.append({"idx": i, "type": "low", "price": c["low"], "time": c["time"]})
    return swings


def determine_trend(candles: List[Dict]) -> str:
    swings = detect_swings(candles)
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


def compute_atr(candles: List[Dict], period: int = ATR_PERIOD) -> Optional[float]:
    """Average True Range over the last `period` closed candles."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else None


def detect_bos(candles: List[Dict]) -> Optional[Dict]:
    if len(candles) < SWING_LOOKBACK * 2 + 2:
        return None
    last_idx = len(candles) - 1
    last = candles[last_idx]
    swings = detect_swings(candles[:last_idx])
    if not swings:
        return None
    rh = next((s for s in reversed(swings) if s["type"] == "high"), None)
    rl = next((s for s in reversed(swings) if s["type"] == "low"), None)
    if rh and last["close"] > rh["price"]:
        return {"direction": "bullish", "bos_idx": last_idx, "bos_candle": last, "broken_swing": rh}
    if rl and last["close"] < rl["price"]:
        return {"direction": "bearish", "bos_idx": last_idx, "bos_candle": last, "broken_swing": rl}
    return None


def find_order_block(candles: List[Dict], bos: Dict) -> Optional[Dict]:
    # OB = last OPPOSITE-colour candle before the BOS (bearish -> up candle,
    # bullish -> down candle) whose range (high-low) sits in $OB_MIN..$OB_MAX.
    # The colour rule anchors the OB at the move's ORIGIN, leaving room for the
    # mandatory FVG between OB and BOS. (Dropping colour pulled the OB right up
    # against the BOS, starved the FVG check, and silenced the bot.)
    direction = bos["direction"]
    for i in range(bos["bos_idx"] - 1, bos["broken_swing"]["idx"], -1):
        c = candles[i]
        rng = c["high"] - c["low"]
        if not (OB_MIN_USD <= rng <= OB_MAX_USD):
            continue
        if direction == "bullish" and c["close"] < c["open"]:
            return {"idx": i, **c}
        if direction == "bearish" and c["close"] > c["open"]:
            return {"idx": i, **c}
    return None


def find_fvg(candles: List[Dict], bos: Dict, ob_idx: int, atr: Optional[float]) -> Optional[Dict]:
    direction = bos["direction"]
    min_gap = FVG_MIN_ATR_MULT * atr if atr and atr > 0 else 0.0
    for i in range(ob_idx, bos["bos_idx"] - 1):
        c1, c3 = candles[i], candles[i + 2]
        if direction == "bullish" and c1["high"] < c3["low"]:
            if (c3["low"] - c1["high"]) >= min_gap:
                return {"low": c1["high"], "high": c3["low"], "idx": i + 1}
        if direction == "bearish" and c1["low"] > c3["high"]:
            if (c1["low"] - c3["high"]) >= min_gap:
                return {"low": c3["high"], "high": c1["low"], "idx": i + 1}
    return None


def compute_levels(direction: str, ob: Dict, atr: Optional[float]) -> Dict:
    # Entry at the NEAR edge of the OB (the edge price retraces to first); stop
    # beyond the FAR edge by SL_BUFFER x ATR (protects against wicks).
    buf = SL_BUFFER_ATR_MULT * atr if atr and atr > 0 else 0.0
    if direction == "bullish":
        entry = ob["high"]          # buy retraces DOWN to the OB high
        sl = ob["low"] - buf
        risk = entry - sl
        target = entry + RR_TARGET * risk
    else:
        entry = ob["low"]           # sell retraces UP to the OB low
        sl = ob["high"] + buf
        risk = sl - entry
        target = entry - RR_TARGET * risk
    return {"entry": entry, "sl": sl, "target": target, "risk": abs(entry - sl)}


def ob_key(direction: str, ob: Dict) -> str:
    return f"{ob['time']}_{direction}"


def detect_session(dt: datetime) -> str:
    """Label the session a signal fired in (UTC).
       London 07-12, NY 12-21, Asia otherwise."""
    h = dt.hour
    if 7 <= h < 12:
        return "London"
    if 12 <= h < 21:
        return "NY"
    return "Asia"


def in_tradeable_session() -> bool:
    h = datetime.now(timezone.utc).hour
    if SESSION_START_UTC <= h < SESSION_END_UTC:
        return True
    if INCLUDE_ASIA and (ASIA_START_UTC <= h < ASIA_END_UTC):
        return True
    return False


# =============================================================================
# CHART
# =============================================================================
def render_setup_chart(candles, bos, ob, fvg, levels, trend_2h, signal_id) -> Optional[bytes]:
    try:
        window = candles[-40:] if len(candles) > 40 else candles
        base = len(candles) - len(window)
        fig, ax = plt.subplots(figsize=(9, 5.5), dpi=110)
        fig.patch.set_facecolor("#1e1e1e"); ax.set_facecolor("#1e1e1e")
        for n, c in enumerate(window):
            up = c["close"] >= c["open"]
            col = "#81b29a" if up else "#e07a5f"
            ax.plot([n, n], [c["low"], c["high"]], color=col, linewidth=0.8, zorder=2)
            lo, hi = min(c["open"], c["close"]), max(c["open"], c["close"])
            ax.add_patch(Rectangle((n - 0.3, lo), 0.6, max(hi - lo, 0.01),
                                   facecolor=col, edgecolor=col, zorder=3))
        ob_x = max(ob["idx"] - base, 0)
        ax.add_patch(Rectangle((ob_x - 0.4, ob["low"]), (len(window) - ob_x) + 0.4,
                               ob["high"] - ob["low"], facecolor="#3a3a52",
                               alpha=0.35, edgecolor="none", zorder=1))
        if fvg:
            ax.add_patch(Rectangle((0, fvg["low"]), len(window), fvg["high"] - fvg["low"],
                                   facecolor="#2d4a4a", alpha=0.25, edgecolor="none", zorder=1))
        ax.axhline(levels["entry"], color="#bfbfbf", linestyle="--", linewidth=1.2,
                   zorder=4, label=f"Entry {levels['entry']:.2f}")
        ax.axhline(levels["sl"], color="#e07a5f", linestyle="--", linewidth=1.2,
                   zorder=4, label=f"Stop {levels['sl']:.2f}")
        ax.axhline(levels["target"], color="#6699cc", linestyle="--", linewidth=1.2,
                   zorder=4, label=f"3R {levels['target']:.2f}")
        ax.set_title(f"{INSTRUMENT_DISPLAY}  {bos['direction'].upper()}  |  "
                     f"2H trend: {trend_2h}  |  {signal_id}",
                     color="#e0e0e0", fontsize=10)
        ax.tick_params(colors="#8e8e93", labelsize=7)
        for s in ax.spines.values():
            s.set_color("#333333")
        ax.legend(loc="upper left", fontsize=7, facecolor="#252526",
                  edgecolor="#333333", labelcolor="#e0e0e0")
        ax.set_xlim(-1, len(window)); ax.margins(y=0.1)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig); buf.seek(0)
        return buf.read()
    except Exception as e:
        log.error(f"chart: {e}"); plt.close("all"); return None


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram(text: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.warning("Telegram creds missing"); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=10)
    except Exception as e:
        log.error(f"send_telegram: {e}")


def grade_keyboard(signal_id: str, selected: Optional[str] = None) -> dict:
    """Inline A/B/C buttons. The currently-selected grade is bracketed,
    e.g. [A] B C, so the chosen one is visible without emojis/color."""
    def label(g):
        return f"[{g}]" if selected == g else g
    return {"inline_keyboard": [[
        {"text": label("A"), "callback_data": f"grade|{signal_id}|A"},
        {"text": label("B"), "callback_data": f"grade|{signal_id}|B"},
        {"text": label("C"), "callback_data": f"grade|{signal_id}|C"},
    ]]}


def send_telegram_photo(image: bytes, caption: str, reply_markup: Optional[dict] = None):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return None
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000],
                "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                          data=data,
                          files={"photo": ("setup.png", image, "image/png")}, timeout=20)
        if r.ok:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"send_photo: {e}")
    return None


def edit_photo_markup(message_id: int, reply_markup: dict):
    """Update just the inline buttons on an already-sent photo message."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and message_id):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
                      json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id,
                            "reply_markup": reply_markup}, timeout=10)
    except Exception as e:
        log.error(f"edit_markup: {e}")


def answer_callback(callback_id: str, text: str = ""):
    """Acknowledge a button tap so Telegram stops the loading spinner."""
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except Exception as e:
        log.error(f"answer_cb: {e}")


def send_telegram_reply(chat_id: str, text: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        log.error(f"reply: {e}")


# =============================================================================
# SIGNAL RECORD  (the full automation-ready contract)
# =============================================================================
def build_record(signal_id, bos, ob, fvg, levels, trend_2h, atr) -> Dict:
    now = datetime.now(timezone.utc)
    risk_atr = round(levels["risk"] / atr, 2) if atr else None
    return {
        # --- filled now by the detector ---
        "id": signal_id,
        "timestamp": now.isoformat(),
        "symbol": INSTRUMENT_DISPLAY,
        "direction": bos["direction"],
        "trend_2h": trend_2h,
        "session": detect_session(now),
        "entry": round(levels["entry"], 2),
        "stop": round(levels["sl"], 2),
        "target": round(levels["target"], 2),
        "risk_distance": round(levels["risk"], 2),   # dollars/points
        "risk_atr": risk_atr,                          # risk as multiple of ATR(14)
        "atr": round(atr, 2) if atr else None,
        "rr_target": RR_TARGET,
        "bos_time": bos["bos_candle"]["time"],
        "ob_time": ob["time"],
        "chart_url": None,                             # set when image hosting lands
        # --- intent for automation (planned, not yet executed) ---
        "risk_percent": RISK_PERCENT,
        "order_expiry_hours": ORDER_EXPIRY_HOURS,
        # --- filled by MetaApi when automation is live ---
        "order_placed": None,
        "fill_status": None,      # pending / filled / cancelled / expired
        "fill_price": None,
        "fill_time": None,
        "outcome": None,          # win / loss / breakeven / no-fill (broker truth)
        "exit_price": None,
        "exit_time": None,
        "r_result": None,         # actual R achieved
        "lot_size": None,
        "pnl": None,
        # --- filled by you (Telegram tap now, or app later) ---
        "grade": None,            # A / B / C — your quality rating of the setup
        "eye_agreement": None,    # agree / skip (richer rating, app)
        "notes": None,
        "telegram_message_id": None,  # for editing the grade buttons after a tap
    }


# =============================================================================
# SCAN
# =============================================================================
def make_signal_id(t: str, direction: str) -> str:
    clean = t.replace(" ", "_").replace(":", "").replace("-", "")
    # NB: both "bullish" and "bearish" start with "b", so direction[0] alone
    # was ambiguous — two same-minute setups of opposite direction would collide.
    # Use a distinct 2-char suffix instead. (Old ids ending in "_b" remain valid;
    # the worker matches trades by the full id, so historical records are unaffected.)
    suffix = "bu" if direction == "bullish" else "be"
    return f"{clean}_{suffix}"


def _record_scan(reason: str):
    """Record why a scan ended — visible via /diag and in logs — so silent
    stretches explain themselves instead of needing guesswork."""
    state["last_scan"] = {"at": datetime.now(timezone.utc).isoformat(), "reason": reason}
    t = state.setdefault("scan_tally", {})
    t[reason] = t.get(reason, 0) + 1
    log.info(f"[scan] {reason}")


def scan():
    try:
        if state.get("paused"):
            _record_scan("paused"); return
        state["last_scan_ts"] = datetime.now(timezone.utc).isoformat()
        reset_daily_counter()
        if not in_tradeable_session():
            _record_scan("outside tradeable session"); return

        candles_15m = fetch_candles(TF_15M, CANDLES_15M)
        if not candles_15m:
            _record_scan("no 15m candles (feed)"); return
        atr = compute_atr(candles_15m)
        if not atr or atr <= 0:
            _record_scan("no ATR"); save_state(); return
        candles_2h = fetch_candles(TF_2H, CANDLES_2H)
        trend_2h = determine_trend(candles_2h) if candles_2h else "unknown"
        state["last_2h_trend"] = trend_2h

        bos = detect_bos(candles_15m)
        if not bos:
            _record_scan("no BOS"); save_state(); return
        ob = find_order_block(candles_15m, bos)
        if not ob:
            _record_scan("BOS ok, no valid OB ($2-$22, opposite-colour)"); save_state(); return
        fvg = find_fvg(candles_15m, bos, ob["idx"], atr)
        if not fvg:
            _record_scan("BOS+OB ok, no FVG (mandatory)"); save_state(); return

        key = ob_key(bos["direction"], ob)
        if key == state.get("last_signal_ob_key"):
            _record_scan("duplicate (same OB as last signal)"); return

        levels = compute_levels(bos["direction"], ob, atr)
        if levels["risk"] <= 0:
            _record_scan("invalid risk (<=0)"); return
        bc = bos["bos_candle"]
        if bos["direction"] == "bullish" and bc["low"] <= levels["sl"]:
            _record_scan("stale: BOS candle already pierced SL"); return
        if bos["direction"] == "bearish" and bc["high"] >= levels["sl"]:
            _record_scan("stale: BOS candle already pierced SL"); return

        signal_id = make_signal_id(bos["bos_candle"]["time"], bos["direction"])
        record = build_record(signal_id, bos, ob, fvg, levels, trend_2h, atr)

        state["signals"].append(record)
        state["last_signal_ob_key"] = key
        state["signals_today"] += 1
        _record_scan(f"SIGNAL fired: {signal_id}")
        sb_upsert_signal(record)   # durable: write this signal row to Supabase
        save_state()               # runtime flags to file

        dir_label = "🟢 BUY" if bos["direction"] == "bullish" else "🔴 SELL"
        atr_line = f"\n<b>Risk in ATR:</b> {record['risk_atr']}x ATR" if record["risk_atr"] else ""
        msg = (f"📐 <b>SETUP · {INSTRUMENT_DISPLAY} · {dir_label}</b>\n"
               f"#setup #{bos['direction']}\n\n"
               f"<b>2H trend (context):</b> {trend_2h}\n"
               f"<b>Session:</b> {record['session']}\n"
               f"<b>Entry (OB):</b> {record['entry']:.2f}\n"
               f"<b>Stop:</b> {record['stop']:.2f}\n"
               f"<b>Target (3R):</b> {record['target']:.2f}\n"
               f"<b>Risk distance:</b> {record['risk_distance']:.2f}{atr_line}\n\n"
               f"<i>ID: {signal_id}</i>\n"
               f"<i>Validate against TradingView. Not auto-traded.</i>")
        send_telegram(msg)

        img = render_setup_chart(candles_15m, bos, ob, fvg, levels, trend_2h, signal_id)
        if img:
            # Host the chart so the web app can show it, then tag the record.
            chart_url = sb_upload_chart(signal_id, img)
            if chart_url:
                record["chart_url"] = chart_url
            msg_id = send_telegram_photo(
                img, f"{INSTRUMENT_DISPLAY} {dir_label} | 2H: {trend_2h} | "
                     f"{record['session']} | E {record['entry']:.2f} / "
                     f"SL {record['stop']:.2f} / 3R {record['target']:.2f} | {signal_id}\n"
                     f"Grade this setup:",
                reply_markup=grade_keyboard(signal_id))
            if msg_id:
                record["telegram_message_id"] = msg_id
            # one durable write covering chart_url and/or telegram_message_id
            if chart_url or msg_id:
                sb_upsert_signal(record)
                save_state()
        log.info(f"SETUP {signal_id} ({bos['direction']}, 2H {trend_2h}, {record['session']})")
    except Exception as e:
        log.exception(f"scan: {e}")


# =============================================================================
# FLASK  (+ CORS so the Vercel-hosted viewer can fetch /signals)
# =============================================================================
app = Flask(__name__)


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def health():
    return jsonify({
        "status": "ok", "bot": "JP Gold Bot v3.2.4 (detector, supabase)",
        "paused": state.get("paused", False),
        "last_scan": state.get("last_scan_ts"),
        "last_2h_trend": state.get("last_2h_trend"),
        "signals_logged": len(state.get("signals", [])),
        "supabase_enabled": SUPABASE_ENABLED,
        "in_session": in_tradeable_session(),
    })


@app.route("/signals")
def signals_endpoint():
    """Full signal log as JSON for the JP Signals viewer."""
    return jsonify({
        "count": len(state.get("signals", [])),
        "symbol": INSTRUMENT_DISPLAY,
        "signals": state.get("signals", []),
    })


@app.route("/update_signal", methods=["POST", "OPTIONS"])
def update_signal_endpoint():
    """Write-back from the JP Signals app: save grade / notes / eye_agreement
    onto an existing signal record by id. Only these user-editable fields can
    be set here — detector and broker fields are never overwritten."""
    from flask import request
    if request.method == "OPTIONS":
        return ("", 204)  # CORS preflight
    data = request.get_json(silent=True) or {}
    sid = data.get("id")
    if not sid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    rec = next((s for s in state.get("signals", []) if s["id"] == sid), None)
    if not rec:
        return jsonify({"ok": False, "error": "signal not found"}), 404

    EDITABLE = {"grade", "eye_agreement", "notes"}
    changed = {}
    for field in EDITABLE:
        if field in data:
            val = data[field]
            if field == "grade" and val not in ("A", "B", "C", None):
                continue
            if field == "eye_agreement" and val not in ("agree", "skip", None):
                continue
            rec[field] = val
            changed[field] = val

    # --- Broker-truth execution fields (Stage 4) -------------------------- #
    # These are NOT user-editable. They may only be written by the execution
    # worker, which proves itself with the shared WORKER_TOKEN. The public app
    # can never set them. Each value is validated before it touches the record.
    EXEC_FIELDS = {
        "order_placed", "fill_status", "fill_price", "fill_time",
        "outcome", "exit_price", "exit_time", "r_result", "lot_size", "pnl",
    }
    if any(f in data for f in EXEC_FIELDS):
        presented = request.headers.get("X-Worker-Token") or data.get("worker_token")
        if not WORKER_TOKEN or presented != WORKER_TOKEN:
            return jsonify({"ok": False, "error": "execution fields require a valid worker token"}), 403
        for field in EXEC_FIELDS:
            if field not in data:
                continue
            val = data[field]
            if field == "outcome" and val not in ("win", "loss", "breakeven", "no-fill", None):
                continue
            if field == "order_placed" and val not in (True, False, None):
                continue
            if field in ("fill_price", "exit_price", "r_result", "lot_size", "pnl") \
                    and val is not None and not isinstance(val, (int, float)):
                continue
            rec[field] = val
            changed[field] = val

    if not changed:
        return jsonify({"ok": False, "error": "no editable fields provided"}), 400
    sb_upsert_signal(rec)   # persist the edit durably
    save_state()
    # If a grade changed and we have the Telegram message, refresh its buttons.
    if "grade" in changed and rec.get("telegram_message_id"):
        edit_photo_markup(rec["telegram_message_id"],
                          grade_keyboard(sid, selected=changed["grade"]))
    log.info(f"UPDATE {sid}: {changed}")
    return jsonify({"ok": True, "id": sid, "updated": changed, "signal": rec})


@app.route("/test_functions")
def test_functions_http():
    out = {}
    c15 = fetch_candles(TF_15M, 50)
    out["fetch_15m"] = bool(c15)
    c2h = fetch_candles(TF_2H, 50)
    out["fetch_2h"] = bool(c2h)
    if c2h:
        out["trend_2h"] = determine_trend(c2h)
    if c15:
        out["swings_15m"] = len(detect_swings(c15))
        out["atr_15m"] = compute_atr(c15)
    out["telegram_creds"] = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    return jsonify(out)


# =============================================================================
# TELEGRAM COMMAND POLLING
# =============================================================================
def handle_command(text: str, chat_id: str):
    cmd = text.strip().split()[0].lower().lstrip("/").split("@")[0]
    if cmd in ("start", "help"):
        reply = ("JP Gold Bot v3.2 — Setup Detector\n\n"
                 "I detect 15M FVG setups, draw them, log them. I don't trade.\n\n"
                 "/status /config /diag /recent /test_functions /pause /resume")
    elif cmd == "status":
        reply = (f"<b>v3.2 Detector — Status</b>\n\n"
                 f"Paused: {'YES' if state.get('paused') else 'no'}\n"
                 f"In session: {'yes' if in_tradeable_session() else 'no'} "
                 f"(UTC hour {datetime.now(timezone.utc).hour})\n"
                 f"Asia enabled: {'yes' if INCLUDE_ASIA else 'no'}\n"
                 f"Last 2H trend: {state.get('last_2h_trend')}\n"
                 f"Last scan: {state.get('last_scan_ts')}\n"
                 f"Setups today: {state.get('signals_today', 0)}\n"
                 f"Setups logged: {len(state.get('signals', []))}")
    elif cmd == "config":
        reply = (f"<b>Config</b>\n"
                 f"Instrument: {INSTRUMENT_DISPLAY}\n"
                 f"Detection: 15M FVG-triggered (BOS+OB+mandatory FVG)\n"
                 f"OB filter: ${OB_MIN_USD:.2f}-${OB_MAX_USD:.2f} high-low, opposite-colour\n"
                 f"FVG min: {FVG_MIN_ATR_MULT}x ATR\n"
                 f"2H trend: context only\n"
                 f"Entry: OB near edge · Stop: OB far edge +{SL_BUFFER_ATR_MULT}x ATR · Target: {RR_TARGET:.0f}R\n"
                 f"ATR period: {ATR_PERIOD}\n"
                 f"Risk %: {RISK_PERCENT}% (for automation sizing)\n"
                 f"Order expiry: {ORDER_EXPIRY_HOURS}h\n"
                 f"Scan: every {SCAN_INTERVAL_SECONDS // 60} min\n"
                 f"Sessions: {SESSION_START_UTC:02d}:00-{SESSION_END_UTC:02d}:00 UTC"
                 f"{' + Asia' if INCLUDE_ASIA else ''}")
    elif cmd == "diag":
        ls = state.get("last_scan") or {}
        tally = state.get("scan_tally") or {}
        if not ls and not tally:
            reply = "No scans recorded yet — give it a scan cycle or two."
        else:
            last_line = (f"{ls.get('reason','?')}  ({ls.get('at','?')[11:19]} UTC)"
                         if ls else "none yet")
            tally_lines = "\n".join(f"  {n}× {r}" for r, n in
                                    sorted(tally.items(), key=lambda kv: -kv[1]))
            reply = (f"<b>Scan diagnostics</b>\n"
                     f"Last scan: {last_line}\n\n"
                     f"<b>Tally (since last restart):</b>\n{tally_lines}")
    elif cmd == "recent":
        sigs = state.get("signals", [])[-5:]
        if not sigs:
            reply = "No setups logged yet."
        else:
            reply = "<b>Recent setups</b>\n" + "\n".join(
                f"{s['id']} · {s['direction'][:4]} · {s.get('session','?')} · "
                f"2H {s['trend_2h']} · E {s['entry']:.2f}/SL {s['stop']:.2f}/"
                f"3R {s['target']:.2f}" for s in reversed(sigs))
    elif cmd == "pause":
        state["paused"] = True; save_state(); reply = "Scanning paused."
    elif cmd == "resume":
        state["paused"] = False; save_state(); reply = "Scanning resumed."
    elif cmd == "test_functions":
        c15 = fetch_candles(TF_15M, 50); c2h = fetch_candles(TF_2H, 50)
        atrv = compute_atr(c15) if c15 else None
        checks = [
            ("Fetch 15M", bool(c15), f"{len(c15) if c15 else 0} bars"),
            ("Fetch 2H", bool(c2h), f"{len(c2h) if c2h else 0} bars"),
            ("15M swings", bool(c15 and detect_swings(c15)),
             f"{len(detect_swings(c15)) if c15 else 0}"),
            ("ATR(14)", bool(atrv), f"{atrv:.2f}" if atrv else "-"),
            ("2H trend", bool(c2h), determine_trend(c2h) if c2h else "-"),
            ("Telegram creds", bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID), ""),
            ("Chart engine", True, "matplotlib"),
            ("/signals", True, f"{len(state.get('signals', []))} logged"),
        ]
        reply = "<b>Test Functions</b>\n\n" + "\n".join(
            f"{'OK' if ok else 'X'} {n}" + (f" - {e}" if e else "")
            for n, ok, e in checks)
    else:
        return
    send_telegram_reply(chat_id, reply)


def handle_grade_callback(cb: dict):
    """Process an A/B/C grade button tap: write grade to the record (last tap
    wins), refresh the buttons to show the selection, ack the tap."""
    cb_id = cb.get("id")
    data = cb.get("data", "")
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "grade":
        if cb_id:
            answer_callback(cb_id)
        return
    _, signal_id, grade = parts
    rec = next((s for s in state.get("signals", []) if s["id"] == signal_id), None)
    if not rec:
        answer_callback(cb_id, "Signal not found")
        return
    rec["grade"] = grade            # last tap wins
    sb_upsert_signal(rec)           # persist the grade durably
    save_state()
    # Refresh buttons so the chosen grade shows as [A]/[B]/[C]
    mid = rec.get("telegram_message_id")
    if mid:
        edit_photo_markup(mid, grade_keyboard(signal_id, selected=grade))
    answer_callback(cb_id, f"Graded {grade}")
    log.info(f"GRADE {signal_id} -> {grade}")


def poll_telegram():
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_TOKEN — polling disabled"); return
    offset = None
    log.info("Telegram polling started")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                             params=params, timeout=60)
            if not r.ok:
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                # Button taps arrive as callback_query, not message
                cb = upd.get("callback_query")
                if cb:
                    handle_grade_callback(cb)
                    continue
                msg = upd.get("message") or upd.get("channel_post")
                if not msg:
                    continue
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/"):
                    handle_command(text, chat_id)
        except Exception as e:
            log.warning(f"poll: {e}")


# =============================================================================
# STARTUP
# =============================================================================
def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(scan, "interval", seconds=SCAN_INTERVAL_SECONDS, id="scan",
                  max_instances=1, coalesce=True)
    sched.start()
    log.info(f"Scheduler: scan every {SCAN_INTERVAL_SECONDS // 60} min")


def start_polling_thread():
    import threading
    threading.Thread(target=poll_telegram, daemon=True).start()


load_state()
start_scheduler()
start_polling_thread()

try:
    send_telegram(
        "🟢 <b>JP Gold Bot v3.2 online</b>\n#startup\n\n"
        "Setup-detector mode, automation-ready records.\n"
        f"Instrument: {INSTRUMENT_DISPLAY}\n"
        f"Scan: every {SCAN_INTERVAL_SECONDS // 60} min · "
        f"Sessions {SESSION_START_UTC:02d}:00-{SESSION_END_UTC:02d}:00 UTC"
        f"{' + Asia' if INCLUDE_ASIA else ''}\n"
        "New: /signals endpoint, ATR risk, session tags.\n"
        "Use /status, /test_functions.")
except Exception as e:
    log.warning(f"startup ping: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
