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
SL_BUFFER_USD = 0.50
RR_TARGET = 3.0
ATR_PERIOD = 14
ORDER_EXPIRY_HOURS = 24   # placed limit must tap within a day (automation rule)

SCAN_INTERVAL_SECONDS = 15 * 60
MAX_LOGGED_SIGNALS = 200

STATE_FILE = "bot_state.json"

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


def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state.update(json.load(f))
            log.info(f"State loaded: {len(state.get('signals', []))} signals")
        except Exception as e:
            log.warning(f"Could not load state: {e}")


def save_state():
    try:
        if len(state["signals"]) > MAX_LOGGED_SIGNALS:
            state["signals"] = state["signals"][-MAX_LOGGED_SIGNALS:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"save_state: {e}")


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
    direction = bos["direction"]
    for i in range(bos["bos_idx"] - 1, bos["broken_swing"]["idx"], -1):
        c = candles[i]
        if direction == "bullish" and c["close"] < c["open"]:
            return {"idx": i, **c}
        if direction == "bearish" and c["close"] > c["open"]:
            return {"idx": i, **c}
    return None


def find_fvg(candles: List[Dict], bos: Dict, ob_idx: int) -> Optional[Dict]:
    direction = bos["direction"]
    for i in range(ob_idx, bos["bos_idx"] - 1):
        c1, c3 = candles[i], candles[i + 2]
        if direction == "bullish" and c1["high"] < c3["low"]:
            return {"low": c1["high"], "high": c3["low"], "idx": i + 1}
        if direction == "bearish" and c1["low"] > c3["high"]:
            return {"low": c3["high"], "high": c1["low"], "idx": i + 1}
    return None


def compute_levels(direction: str, ob: Dict) -> Dict:
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


def send_telegram_photo(image: bytes, caption: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                      data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000],
                            "parse_mode": "HTML"},
                      files={"photo": ("setup.png", image, "image/png")}, timeout=20)
    except Exception as e:
        log.error(f"send_photo: {e}")


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
        # --- filled by you in the viewer ---
        "eye_agreement": None,    # agree / skip
        "notes": None,
    }


# =============================================================================
# SCAN
# =============================================================================
def make_signal_id(t: str, direction: str) -> str:
    clean = t.replace(" ", "_").replace(":", "").replace("-", "")
    return f"{clean}_{direction[0]}"


def scan():
    try:
        if state.get("paused"):
            return
        state["last_scan_ts"] = datetime.now(timezone.utc).isoformat()
        reset_daily_counter()
        if not in_tradeable_session():
            return

        candles_15m = fetch_candles(TF_15M, CANDLES_15M)
        if not candles_15m:
            return
        candles_2h = fetch_candles(TF_2H, CANDLES_2H)
        trend_2h = determine_trend(candles_2h) if candles_2h else "unknown"
        state["last_2h_trend"] = trend_2h

        bos = detect_bos(candles_15m)
        if not bos:
            save_state(); return
        ob = find_order_block(candles_15m, bos)
        if not ob:
            save_state(); return
        fvg = find_fvg(candles_15m, bos, ob["idx"])
        if not fvg:
            log.info("BOS+OB but no FVG -> skip (FVG mandatory)")
            save_state(); return

        key = ob_key(bos["direction"], ob)
        if key == state.get("last_signal_ob_key"):
            return

        levels = compute_levels(bos["direction"], ob)
        if levels["risk"] <= 0:
            return
        bc = bos["bos_candle"]
        if bos["direction"] == "bullish" and bc["low"] <= levels["sl"]:
            return
        if bos["direction"] == "bearish" and bc["high"] >= levels["sl"]:
            return

        atr = compute_atr(candles_15m)
        signal_id = make_signal_id(bos["bos_candle"]["time"], bos["direction"])
        record = build_record(signal_id, bos, ob, fvg, levels, trend_2h, atr)

        state["signals"].append(record)
        state["last_signal_ob_key"] = key
        state["signals_today"] += 1
        save_state()

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
            send_telegram_photo(
                img, f"{INSTRUMENT_DISPLAY} {dir_label} | 2H: {trend_2h} | "
                     f"{record['session']} | E {record['entry']:.2f} / "
                     f"SL {record['stop']:.2f} / 3R {record['target']:.2f} | {signal_id}")
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
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def health():
    return jsonify({
        "status": "ok", "bot": "JP Gold Bot v3.2 (detector)",
        "paused": state.get("paused", False),
        "last_scan": state.get("last_scan_ts"),
        "last_2h_trend": state.get("last_2h_trend"),
        "signals_logged": len(state.get("signals", [])),
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
                 "/status /config /recent /test_functions /pause /resume")
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
                 f"2H trend: context only\n"
                 f"Entry: OB · Stop: OB inval +${SL_BUFFER_USD} · Target: {RR_TARGET:.0f}R\n"
                 f"ATR period: {ATR_PERIOD}\n"
                 f"Risk %: {RISK_PERCENT}% (for automation sizing)\n"
                 f"Order expiry: {ORDER_EXPIRY_HOURS}h\n"
                 f"Scan: every {SCAN_INTERVAL_SECONDS // 60} min\n"
                 f"Sessions: {SESSION_START_UTC:02d}:00-{SESSION_END_UTC:02d}:00 UTC"
                 f"{' + Asia' if INCLUDE_ASIA else ''}")
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
