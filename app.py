"""
=============================================================================
JP GOLD BOT v3.2 — Setup Detector (no trade tracking)
=============================================================================
Philosophy:
  This bot is NOT a trader and NOT a trade tracker. It is a SETUP DETECTOR.
  Its single job: when a clean textbook SMC setup forms on the 15M chart,
  draw it, log it, and show it. The human validates each image against
  TradingView. We are training the detector's eye, not trading its output.

Strategy (locked):
  - 2H trend = CONTEXT ONLY (recorded in each signal, never filters anything)
  - 15M detection, in this order:
      1. A structure break (BOS) on the most recent closed candle
      2. The impulse that broke structure MUST contain an FVG (mandatory)
      3. Mark the Order Block (last opposing candle before the impulse)
  - Entry = OB open.  Stop = OB invalidation (+buffer).  Target = 3R fixed.
  - NO 2H swing targets. NO grading filter. NO trade tracking. NO add-on logic.
  - Every valid setup is sent once (deduped by order block). Human filters by eye.

Output per signal:
  - Telegram text: direction, 2H trend (context), entry/stop/target, signal ID
  - Annotated chart PNG: candles + OB box + grey dashed entry + red dashed stop
    + blue dashed 3R target, with context burned into the image

Operations:
  - XAUUSD only (symbol configurable via MT_SYMBOL for future broker use)
  - Scans once per closed 15M candle (every 15 min)
  - London + NY sessions (07:00-22:00 UTC)
  - Commands: /start /help /status /config /recent /test_functions /pause /resume
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
matplotlib.use("Agg")  # headless rendering on Render
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

# =============================================================================
# CONFIG
# =============================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "")
PORT = int(os.getenv("PORT", "10000"))

INSTRUMENT_DISPLAY = os.getenv("MT_SYMBOL", "XAUUSD")
INSTRUMENT_API = "XAU/USD"  # Twelve Data symbol

# Sessions (UTC)
SESSION_START_UTC = 7
SESSION_END_UTC = 22

# Timeframes
TF_2H = "2h"
TF_15M = "15min"
CANDLES_2H = 80
CANDLES_15M = 60        # enough history for swings + impulse + chart context

# Strategy params
SWING_LOOKBACK = 3      # bars each side for a swing point
SL_BUFFER_USD = 0.50    # buffer beyond OB invalidation
RR_TARGET = 3.0         # fixed 3R target

# Scan cadence: once per closed 15M candle.
SCAN_INTERVAL_SECONDS = 15 * 60

# How many recent signals to keep in state for /recent
MAX_LOGGED_SIGNALS = 100

STATE_FILE = "bot_state.json"

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("jp-gold-bot")
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# =============================================================================
# STATE  (minimal — no trades, just dedup memory + a signal log)
# =============================================================================
state: Dict[str, Any] = {
    "paused": False,
    "last_scan_ts": None,
    "last_2h_trend": None,
    "last_signal_ob_key": None,   # dedup: OB of the most recent fired setup
    "signals": [],                # rolling log of fired signals (for /recent)
    "signals_today": 0,
    "today_date": None,
    "bot_started_at": datetime.now(timezone.utc).isoformat(),
}


def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state.update(json.load(f))
            log.info(f"State loaded: {len(state.get('signals', []))} logged signals")
        except Exception as e:
            log.warning(f"Could not load state: {e}. Fresh start.")


def save_state():
    try:
        if len(state["signals"]) > MAX_LOGGED_SIGNALS:
            state["signals"] = state["signals"][-MAX_LOGGED_SIGNALS:]
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
# DATA FETCHING  (drops the forming candle at source)
# =============================================================================
def fetch_candles(timeframe: str, count: int) -> Optional[List[Dict]]:
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
            return None
        candles = []
        for v in reversed(values):  # oldest first
            candles.append({
                "time": v["datetime"],
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
            })
        # Drop the still-forming candle so every function sees only CLOSED bars.
        if len(candles) > 1:
            candles = candles[:-1]
        return candles
    except Exception as e:
        log.error(f"fetch_candles({timeframe}) failed: {e}")
        return None


# =============================================================================
# STRUCTURE DETECTION  (pure deterministic candle math — no AI, no guessing)
# =============================================================================
def detect_swings(candles: List[Dict], lookback: int = SWING_LOOKBACK) -> List[Dict]:
    """Fractal swing points: a swing high has `lookback` lower highs on each
    side; a swing low has `lookback` higher lows on each side."""
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
    """2H trend by HH/HL vs LH/LL on recent swings. CONTEXT ONLY."""
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


def detect_bos(candles: List[Dict]) -> Optional[Dict]:
    """Break of structure on the most recent CLOSED candle.
    Bullish: close above the most recent swing high.
    Bearish: close below the most recent swing low."""
    if len(candles) < SWING_LOOKBACK * 2 + 2:
        return None
    last_idx = len(candles) - 1
    last = candles[last_idx]
    swings = detect_swings(candles[:last_idx])
    if not swings:
        return None
    recent_high = next((s for s in reversed(swings) if s["type"] == "high"), None)
    recent_low = next((s for s in reversed(swings) if s["type"] == "low"), None)
    if recent_high and last["close"] > recent_high["price"]:
        return {"direction": "bullish", "bos_idx": last_idx,
                "bos_candle": last, "broken_swing": recent_high}
    if recent_low and last["close"] < recent_low["price"]:
        return {"direction": "bearish", "bos_idx": last_idx,
                "bos_candle": last, "broken_swing": recent_low}
    return None


def find_order_block(candles: List[Dict], bos: Dict) -> Optional[Dict]:
    """OB = last opposing-color candle inside the impulse leg, scanning back
    from the BOS candle toward the broken swing."""
    direction = bos["direction"]
    bos_idx = bos["bos_idx"]
    broken_idx = bos["broken_swing"]["idx"]
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
    """3-candle FVG inside the impulse leg between the OB and the BOS candle.
    Bullish FVG: gap between candle[i] high and candle[i+2] low.
    Bearish FVG: gap between candle[i] low and candle[i+2] high.
    MANDATORY — no FVG, no setup."""
    direction = bos["direction"]
    bos_idx = bos["bos_idx"]
    for i in range(ob_idx, bos_idx - 1):
        c1 = candles[i]
        c3 = candles[i + 2]
        if direction == "bullish" and c1["high"] < c3["low"]:
            return {"low": c1["high"], "high": c3["low"], "idx": i + 1}
        if direction == "bearish" and c1["low"] > c3["high"]:
            return {"low": c3["high"], "high": c1["low"], "idx": i + 1}
    return None


def compute_levels(direction: str, ob: Dict) -> Dict:
    """Entry at OB open; stop beyond OB invalidation + buffer; target = 3R."""
    entry = ob["open"]
    if direction == "bullish":
        sl = ob["low"] - SL_BUFFER_USD
        risk = entry - sl
        target = entry + RR_TARGET * risk
    else:
        sl = ob["high"] + SL_BUFFER_USD
        risk = sl - entry
        target = entry - RR_TARGET * risk
    return {"entry": entry, "sl": sl, "target": target, "risk": abs(entry - sl)}


def ob_key(direction: str, ob: Dict) -> str:
    """Dedup key: a setup is 'the same' if it uses the same OB candle time and
    direction. Prevents re-firing the same setup on consecutive scans."""
    return f"{ob['time']}_{direction}"


# =============================================================================
# CHART IMAGE
# =============================================================================
def render_setup_chart(candles: List[Dict], bos: Dict, ob: Dict, fvg: Dict,
                       levels: Dict, trend_2h: str, signal_id: str) -> Optional[bytes]:
    """Render an annotated candlestick PNG:
       OB shaded box, grey dashed entry, red dashed stop, blue dashed 3R target,
       FVG light band, with full context burned into the title."""
    try:
        # Show a window around the setup (last ~40 candles).
        window = candles[-40:] if len(candles) > 40 else candles
        base_idx = len(candles) - len(window)

        fig, ax = plt.subplots(figsize=(9, 5.5), dpi=110)
        fig.patch.set_facecolor("#1e1e1e")
        ax.set_facecolor("#1e1e1e")

        for n, c in enumerate(window):
            up = c["close"] >= c["open"]
            color = "#81b29a" if up else "#e07a5f"
            # wick
            ax.plot([n, n], [c["low"], c["high"]], color=color, linewidth=0.8, zorder=2)
            # body
            lo = min(c["open"], c["close"])
            hi = max(c["open"], c["close"])
            ax.add_patch(Rectangle((n - 0.3, lo), 0.6, max(hi - lo, 0.01),
                                   facecolor=color, edgecolor=color, zorder=3))

        # OB box (spans from its candle to the right edge)
        ob_x = ob["idx"] - base_idx
        if ob_x < 0:
            ob_x = 0
        ax.add_patch(Rectangle((ob_x - 0.4, ob["low"]), (len(window) - ob_x) + 0.4,
                               ob["high"] - ob["low"],
                               facecolor="#3a3a52", alpha=0.35, edgecolor="none", zorder=1))

        # FVG band
        if fvg:
            ax.add_patch(Rectangle((0, fvg["low"]), len(window), fvg["high"] - fvg["low"],
                                   facecolor="#2d4a4a", alpha=0.25, edgecolor="none", zorder=1))

        # Level lines
        ax.axhline(levels["entry"], color="#bfbfbf", linestyle="--", linewidth=1.2,
                   zorder=4, label=f"Entry {levels['entry']:.2f}")
        ax.axhline(levels["sl"], color="#e07a5f", linestyle="--", linewidth=1.2,
                   zorder=4, label=f"Stop {levels['sl']:.2f}")
        ax.axhline(levels["target"], color="#6699cc", linestyle="--", linewidth=1.2,
                   zorder=4, label=f"3R {levels['target']:.2f}")

        ax.set_title(
            f"{INSTRUMENT_DISPLAY}  {bos['direction'].upper()}  |  2H trend: {trend_2h}  |  {signal_id}",
            color="#e0e0e0", fontsize=10)
        ax.tick_params(colors="#8e8e93", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#333333")
        ax.legend(loc="upper left", fontsize=7, facecolor="#252526",
                  edgecolor="#333333", labelcolor="#e0e0e0")
        ax.set_xlim(-1, len(window))
        ax.margins(y=0.1)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        log.error(f"chart render failed: {e}")
        plt.close("all")
        return None


# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram creds missing — would have sent:\n" + text[:200])
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
        if not r.ok:
            log.error(f"Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")


def send_telegram_photo(image: bytes, caption: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram creds missing — photo not sent")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000],
                  "parse_mode": "HTML"},
            files={"photo": ("setup.png", image, "image/png")},
            timeout=20)
        if not r.ok:
            log.error(f"Telegram photo failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Telegram photo error: {e}")


# =============================================================================
# SESSION + SCAN
# =============================================================================
def in_session() -> bool:
    h = datetime.now(timezone.utc).hour
    return SESSION_START_UTC <= h < SESSION_END_UTC


def make_signal_id(bos_candle_time: str, direction: str) -> str:
    clean = bos_candle_time.replace(" ", "_").replace(":", "").replace("-", "")
    return f"{clean}_{direction[0]}"


def scan():
    """One scan per closed 15M candle. Detect -> draw -> log -> notify. No tracking."""
    try:
        if state.get("paused"):
            return
        state["last_scan_ts"] = datetime.now(timezone.utc).isoformat()
        reset_daily_counter()

        if not in_session():
            return

        candles_15m = fetch_candles(TF_15M, CANDLES_15M)
        if not candles_15m:
            log.warning("scan: no 15M candles")
            return

        # 2H trend — context only
        candles_2h = fetch_candles(TF_2H, CANDLES_2H)
        trend_2h = determine_trend(candles_2h) if candles_2h else "unknown"
        state["last_2h_trend"] = trend_2h

        # 1) Structure break on the latest closed candle
        bos = detect_bos(candles_15m)
        if not bos:
            save_state()
            return

        # 2) Order block
        ob = find_order_block(candles_15m, bos)
        if not ob:
            log.info("scan: BOS but no OB -> skip")
            save_state()
            return

        # 3) FVG is MANDATORY
        fvg = find_fvg(candles_15m, bos, ob["idx"])
        if not fvg:
            log.info("scan: BOS+OB but no FVG -> skip (FVG mandatory)")
            save_state()
            return

        # Dedup: same OB as last fired setup? skip.
        key = ob_key(bos["direction"], ob)
        if key == state.get("last_signal_ob_key"):
            return

        levels = compute_levels(bos["direction"], ob)
        if levels["risk"] <= 0:
            log.info("scan: invalid risk -> skip")
            return

        # Dead-on-arrival guard: price already through OB invalidation on BOS candle
        bc = bos["bos_candle"]
        if bos["direction"] == "bullish" and bc["low"] <= levels["sl"]:
            log.info("scan: OB already invalidated -> skip")
            return
        if bos["direction"] == "bearish" and bc["high"] >= levels["sl"]:
            log.info("scan: OB already invalidated -> skip")
            return

        signal_id = make_signal_id(bos["bos_candle"]["time"], bos["direction"])
        direction_label = "🟢 BUY" if bos["direction"] == "bullish" else "🔴 SELL"

        # Build + log the signal record
        record = {
            "id": signal_id,
            "time": datetime.now(timezone.utc).isoformat(),
            "direction": bos["direction"],
            "trend_2h": trend_2h,
            "entry": levels["entry"],
            "sl": levels["sl"],
            "target": levels["target"],
            "risk": levels["risk"],
            "bos_time": bos["bos_candle"]["time"],
            "ob_time": ob["time"],
        }
        state["signals"].append(record)
        state["last_signal_ob_key"] = key
        state["signals_today"] += 1
        save_state()

        # Telegram text
        msg = (
            f"📐 <b>SETUP · {INSTRUMENT_DISPLAY} · {direction_label}</b>\n"
            f"#setup #{bos['direction']}\n\n"
            f"<b>2H trend (context):</b> {trend_2h}\n"
            f"<b>Entry (OB):</b> {levels['entry']:.2f}\n"
            f"<b>Stop:</b> {levels['sl']:.2f}\n"
            f"<b>Target (3R):</b> {levels['target']:.2f}\n"
            f"<b>Risk distance:</b> {levels['risk']:.2f}\n\n"
            f"<i>ID: {signal_id}</i>\n"
            f"<i>Validate against TradingView. Not auto-traded.</i>"
        )
        send_telegram(msg)

        # Annotated chart
        img = render_setup_chart(candles_15m, bos, ob, fvg, levels, trend_2h, signal_id)
        if img:
            cap = (f"{INSTRUMENT_DISPLAY} {direction_label} | 2H: {trend_2h} | "
                   f"E {levels['entry']:.2f} / SL {levels['sl']:.2f} / "
                   f"3R {levels['target']:.2f} | {signal_id}")
            send_telegram_photo(img, cap)

        log.info(f"SETUP fired: {signal_id} ({bos['direction']}, 2H {trend_2h})")

    except Exception as e:
        log.exception(f"scan() error: {e}")


# =============================================================================
# FLASK
# =============================================================================
app = Flask(__name__)


@app.route("/")
def health():
    return jsonify({
        "status": "ok",
        "bot": "JP Gold Bot v3.2 (detector)",
        "paused": state.get("paused", False),
        "last_scan": state.get("last_scan_ts"),
        "last_2h_trend": state.get("last_2h_trend"),
        "signals_logged": len(state.get("signals", [])),
        "in_session": in_session(),
    })


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
        bos = detect_bos(c15)
        out["bos_now"] = bool(bos)
    out["telegram_creds"] = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    return jsonify(out)


# =============================================================================
# TELEGRAM COMMANDS (long-polling via getUpdates in a background thread)
# =============================================================================
def handle_command(text: str, chat_id: str):
    cmd = text.strip().split()[0].lower().lstrip("/")
    cmd = cmd.split("@")[0]
    if cmd in ("start", "help"):
        reply = (
            "JP Gold Bot v3.2 — Setup Detector\n\n"
            "I detect textbook 15M SMC setups (FVG-triggered), draw them, "
            "and log them. I do NOT trade or track trades. You validate each "
            "image against TradingView.\n\n"
            "/status — diagnostics\n"
            "/config — parameters\n"
            "/recent — last few setups\n"
            "/test_functions — smoke test\n"
            "/pause /resume — control scanning"
        )
    elif cmd == "status":
        reply = (
            f"<b>v3.2 Detector — Status</b>\n\n"
            f"Paused: {'YES' if state.get('paused') else 'no'}\n"
            f"In session: {'yes' if in_session() else 'no'} "
            f"(UTC hour {datetime.now(timezone.utc).hour})\n"
            f"Last 2H trend: {state.get('last_2h_trend')}\n"
            f"Last scan: {state.get('last_scan_ts')}\n"
            f"Setups today: {state.get('signals_today', 0)}\n"
            f"Setups logged total: {len(state.get('signals', []))}"
        )
    elif cmd == "config":
        reply = (
            f"<b>Config</b>\n"
            f"Instrument: {INSTRUMENT_DISPLAY}\n"
            f"Detection: 15M FVG-triggered (BOS + OB + mandatory FVG)\n"
            f"2H trend: context only (no filter)\n"
            f"Entry: OB open · Stop: OB invalidation +${SL_BUFFER_USD} · "
            f"Target: {RR_TARGET:.0f}R\n"
            f"Scan: every {SCAN_INTERVAL_SECONDS // 60} min (per closed candle)\n"
            f"Sessions: {SESSION_START_UTC:02d}:00-{SESSION_END_UTC:02d}:00 UTC\n"
            f"Trade tracking: NONE (detector only)"
        )
    elif cmd == "recent":
        sigs = state.get("signals", [])[-5:]
        if not sigs:
            reply = "No setups logged yet."
        else:
            lines = ["<b>Recent setups</b>"]
            for s in reversed(sigs):
                lines.append(
                    f"{s['id']} · {s['direction'][:4]} · 2H {s['trend_2h']} · "
                    f"E {s['entry']:.2f}/SL {s['sl']:.2f}/3R {s['target']:.2f}")
            reply = "\n".join(lines)
    elif cmd == "pause":
        state["paused"] = True
        save_state()
        reply = "⏸ Scanning paused."
    elif cmd == "resume":
        state["paused"] = False
        save_state()
        reply = "▶️ Scanning resumed."
    elif cmd == "test_functions":
        c15 = fetch_candles(TF_15M, 50)
        c2h = fetch_candles(TF_2H, 50)
        checks = [
            ("Fetch 15M", bool(c15), f"{len(c15) if c15 else 0} bars"),
            ("Fetch 2H", bool(c2h), f"{len(c2h) if c2h else 0} bars"),
            ("15M swings", bool(c15 and detect_swings(c15)),
             f"{len(detect_swings(c15)) if c15 else 0}"),
            ("2H trend", bool(c2h), determine_trend(c2h) if c2h else "-"),
            ("Telegram creds", bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID), ""),
            ("Chart engine", True, "matplotlib ready"),
        ]
        reply = "<b>Test Functions</b>\n\n" + "\n".join(
            f"{'✅' if ok else '❌'} {name}" + (f" — {extra}" if extra else "")
            for name, ok, extra in checks)
    else:
        return  # unknown command: ignore
    send_telegram_reply(chat_id, reply)


def send_telegram_reply(chat_id: str, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        log.error(f"reply failed: {e}")


def poll_telegram():
    """Simple long-poll loop for commands. Runs in a background thread."""
    if not TELEGRAM_TOKEN:
        log.warning("No TELEGRAM_TOKEN — command polling disabled")
        return
    offset = None
    log.info("Telegram command polling started")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params, timeout=60)
            if not r.ok:
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post")
                if not msg:
                    continue
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/"):
                    handle_command(text, chat_id)
        except Exception as e:
            log.warning(f"poll error: {e}")


# =============================================================================
# STARTUP
# =============================================================================
def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(scan, "interval", seconds=SCAN_INTERVAL_SECONDS, id="scan",
                  max_instances=1, coalesce=True)
    sched.start()
    log.info(f"Scheduler started: scan every {SCAN_INTERVAL_SECONDS // 60} min")


def start_polling_thread():
    import threading
    t = threading.Thread(target=poll_telegram, daemon=True)
    t.start()


load_state()
start_scheduler()
start_polling_thread()

try:
    send_telegram(
        "🟢 <b>JP Gold Bot v3.2 online</b>\n#startup\n\n"
        "Setup-detector mode. I find 15M FVG setups, draw them, log them. "
        "No trade tracking.\n"
        f"Instrument: {INSTRUMENT_DISPLAY}\n"
        f"Scan: every {SCAN_INTERVAL_SECONDS // 60} min · "
        f"Sessions {SESSION_START_UTC:02d}:00-{SESSION_END_UTC:02d}:00 UTC\n"
        "Use /status, /test_functions."
    )
except Exception as e:
    log.warning(f"startup ping failed: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
