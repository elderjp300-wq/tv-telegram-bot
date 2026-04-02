from datetime import datetime, timezone
from flask import Flask, request
import requests
import os
import base64

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
EXCHANGE_API_KEY = os.environ.get("EXCHANGE_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY")

def send_telegram(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload)

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id})

def is_trading_session():
    hour = datetime.now(timezone.utc).hour
    return (7 <= hour < 12) or (12 <= hour < 17)

def get_session_label():
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour < 12:
        return "London 🇬🇧"
    elif 12 <= hour < 17:
        return "New York 🇺🇸"
    return "Off-Session 🌙"

def get_next_session():
    hour = datetime.now(timezone.utc).hour
    if hour < 7:
        return f"London opens in {7 - hour}h"
    elif hour < 12:
        return "London session active"
    elif hour < 17:
        return "New York session active"
    else:
        return f"London opens in {31 - hour}h"

def main_menu():
    session_status = "🟢 LIVE" if is_trading_session() else "🔴 CLOSED"
    return {
        "inline_keyboard": [
            [{"text": f"⏰ {session_status} — {get_session_label()}", "callback_data": "session_info"}],
            [
                {"text": "🇪🇺 EURUSD", "callback_data": "price_EURUSD"},
                {"text": "🇯🇵 USDJPY", "callback_data": "price_USDJPY"}
            ],
            [
                {"text": "🇬🇧 GBPUSD", "callback_data": "price_GBPUSD"},
                {"text": "🥇 GOLD",    "callback_data": "price_XAUUSD"}
            ],
            [
                {"text": "📡 Scan All Pairs", "callback_data": "scan_all"},
                {"text": "📓 Trade Log",      "callback_data": "history"}
            ],
            [
                {"text": "⚔️ My Entry Rules", "callback_data": "rules"},
                {"text": "✅ A+ Checklist",   "callback_data": "checklist"}
            ]
        ]
    }

def back_menu():
    return {"inline_keyboard": [[{"text": "🏠 Back to Dashboard", "callback_data": "dashboard"}]]}

def get_forex_price(pair):
    if pair == "XAUUSD":
        try:
            url = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
            res = requests.get(url, timeout=10).json()
            return round(res[0]["spreadProfilePrices"][0]["ask"], 2)
        except:
            return None
    pair_map = {"EURUSD": ("EUR","USD"), "USDJPY": ("USD","JPY"), "GBPUSD": ("GBP","USD")}
    base, quote = pair_map[pair]
    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/pair/{base}/{quote}"
    try:
        res = requests.get(url, timeout=10).json()
        return res["conversion_rate"]
    except:
        return None

def get_candles(pair, interval="4h", outputsize=20):
    symbol_map = {"EURUSD":"EUR/USD","USDJPY":"USD/JPY","GBPUSD":"GBP/USD","XAUUSD":"XAU/USD"}
    symbol = symbol_map.get(pair)
    if not symbol:
        return None
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVE_DATA_KEY}
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res.get("status") == "error":
            return None
        candles = res.get("values", [])
        candles.reverse()
        return candles
    except:
        return None

def get_swings(highs, lows, lookback=2):
    """
    Detects real swing highs and swing lows from OHLC data.
    A swing high = highest point with lookback candles on each side.
    A swing low  = lowest point with lookback candles on each side.
    Returns lists of (index, price) tuples.
    """
    swing_highs = []
    swing_lows  = []
    for i in range(lookback, len(highs) - lookback):
        if highs[i] == max(highs[i - lookback: i + lookback + 1]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[i - lookback: i + lookback + 1]):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def detect_structure(candles):
    """
    Phase 2 — Real swing-based structure detection.
    Uses HH/HL/LH/LL logic instead of % price change.
    BOS and CHoCH are now based on actual structure breaks.
    """
    if not candles or len(candles) < 10:
        return None

    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]
    closes = [float(c["close"]) for c in candles]

    current_price = closes[-1]
    recent_high   = max(highs[-10:])
    recent_low    = min(lows[-10:])

    swing_highs, swing_lows = get_swings(highs, lows)

    # Need at least 2 swing highs and 2 swing lows for structure
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        # Fallback: not enough swings yet — mark as ranging
        trend = "Ranging"
        bos   = "None"
        choch = "None"
    else:
        last_high = swing_highs[-1][1]
        prev_high = swing_highs[-2][1]
        last_low  = swing_lows[-1][1]
        prev_low  = swing_lows[-2][1]

        # REAL TREND: HH+HL = Bullish, LH+LL = Bearish, else Ranging
        if last_high > prev_high and last_low > prev_low:
            trend = "Bullish"
        elif last_high < prev_high and last_low < prev_low:
            trend = "Bearish"
        else:
            trend = "Ranging"

        # REAL BOS: price breaks the previous structural high/low
        bos = "None"
        if trend == "Bullish" and current_price > prev_high:
            bos = "Bullish BOS (broke above prev swing high)"
        elif trend == "Bearish" and current_price < prev_low:
            bos = "Bearish BOS (broke below prev swing low)"

        # REAL CHoCH: price breaks AGAINST the trend — first sign of reversal
        choch = "None"
        if trend == "Bearish" and current_price > prev_high:
            choch = "Bullish CHoCH (bearish trend broken to upside)"
        elif trend == "Bullish" and current_price < prev_low:
            choch = "Bearish CHoCH (bullish trend broken to downside)"

    # Fib zone (Premium / Discount / Equilibrium)
    price_range = recent_high - recent_low
    if price_range > 0:
        fib_position = (current_price - recent_low) / price_range
        if fib_position > 0.618:
            zone = f"Premium ({round(fib_position * 100, 1)}% of range)"
        elif fib_position < 0.382:
            zone = f"Discount ({round(fib_position * 100, 1)}% of range)"
        else:
            zone = f"Equilibrium ({round(fib_position * 100, 1)}% of range)"
    else:
        zone = "Ranging (no clear range)"

    return {
        "trend": trend,
        "current_price": round(current_price, 5),
        "recent_high": round(recent_high, 5),
        "recent_low": round(recent_low, 5),
        "bos": bos,
        "choch": choch,
        "zone": zone
    }


def confirm_entry_15m(pair):
    """
    Phase 2 — Checks 15M candles for a real CHoCH confirmation.
    This is the LTF entry trigger — bot checks it automatically
    instead of just telling you to check manually.
    Returns (confirmed: bool, reason: str)
    """
    candles = get_candles(pair, interval="15min", outputsize=30)
    if not candles:
        return False, "No 15M data available"

    structure = detect_structure(candles)
    if not structure:
        return False, "15M structure unclear — not enough swings"

    if structure["choch"] != "None":
        return True, f"15M CHoCH confirmed: {structure['choch']}"

    if structure["bos"] != "None":
        return True, f"15M BOS confirmed: {structure['bos']}"

    return False, "No 15M CHoCH or BOS yet — wait for LTF trigger"

def multi_timeframe_confluence(pair):
    daily = get_candles(pair, interval="1day", outputsize=20)
    h4    = get_candles(pair, interval="4h",   outputsize=20)
    h1    = get_candles(pair, interval="1h",   outputsize=20)
    if not daily or not h4 or not h1:
        return {"verdict": "NO TRADE", "reason": "Could not fetch all timeframes"}
    s_daily = detect_structure(daily)
    s_h4    = detect_structure(h4)
    s_h1    = detect_structure(h1)
    if not s_daily or not s_h4 or not s_h1:
        return {"verdict": "NO TRADE", "reason": "Structure detection failed on one or more timeframes"}
    d_trend  = s_daily["trend"]
    h4_trend = s_h4["trend"]
    h1_zone  = s_h1["zone"].lower()
    h4_bos   = s_h4["bos"]
    if d_trend == "Bullish" and h4_trend == "Bullish" and "discount" in h1_zone:
        verdict = "A+"
        reason  = "Daily Bullish + 4H Bullish BOS + 1H in Discount"
    elif d_trend == "Bearish" and h4_trend == "Bearish" and "premium" in h1_zone:
        verdict = "A+"
        reason  = "Daily Bearish + 4H Bearish BOS + 1H in Premium"
    elif d_trend == h4_trend and d_trend != "Ranging":
        verdict = "WATCHLIST"
        reason  = f"Daily + 4H both {d_trend} but 1H not at entry zone yet"
    elif d_trend == "Ranging":
        verdict = "NO TRADE"
        reason  = "Daily trend is ranging — no macro bias"
    elif d_trend != h4_trend and d_trend != "Ranging" and h4_trend != "Ranging":
        verdict = "NO TRADE"
        reason  = f"Daily {d_trend} conflicts with 4H {h4_trend}"
    else:
        verdict = "NO TRADE"
        reason  = "No clear confluence across timeframes"
    return {"verdict": verdict, "reason": reason, "daily_trend": d_trend, "h4_trend": h4_trend, "h1_zone": s_h1["zone"], "h4_bos": h4_bos}

def ask_groq(prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": "You are a sharp SMC/ICT forex trading analyst. Keep responses short, smart and actionable. Reject low-quality setups. Only speak if high probability."},
            {"role": "user", "content": prompt}
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=25)
        return res.json()["choices"][0]["message"]["content"]
    except:
        return None

def ask_groq_chat(message):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 150,
        "messages": [
            {"role": "system", "content": "You are JP's personal trading assistant bot called JP Bot. You are sharp, friendly, motivating and brief. JP is a forex trader working toward running a hedge fund. He trades EURUSD, USDJPY, GBPUSD and Gold using SMC/ICT methodology on a prop firm challenge. Keep all replies under 3 sentences. Be real, not robotic."},
            {"role": "user", "content": message}
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=25)
        return res.json()["choices"][0]["message"]["content"]
    except:
        return None

def ask_groq_image(base64_image):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}", "detail": "low"}},
            {"type": "text", "text": "You are a sharp SMC/ICT trading analyst. Analyse this chart and give me: 1. Trend bias 2. Key zone to watch 3. Entry condition 4. One risk note. Keep it short and sharp."}
        ]}]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=25)
        data = res.json()
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        else:
            return f"Model error: {data.get('error', {}).get('message', 'Unknown')}"
    except Exception as e:
        return f"Request failed: {str(e)}"

def get_file_base64(file_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    res = requests.get(url).json()
    file_path = res["result"]["file_path"]
    file_url  = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    img_data  = requests.get(file_url).content
    return base64.b64encode(img_data).decode("utf-8")

def build_smc_prompt(pair, structure):
    return f"""You are a strict institutional SMC/ICT trading analyst.
REAL 4H data for {pair}:
- Price: {structure['current_price']} | Trend: {structure['trend']}
- Swing High: {structure['recent_high']} | Swing Low: {structure['recent_low']}
- BOS: {structure['bos']} | CHoCH: {structure['choch']}
- Zone: {structure['zone']} | Session: {get_session_label()}
Give: 1.Bias 2.Key zone 3.Entry condition 4.Risk note
Rules: NO TRADE if ranging, if buying in premium, if selling in discount. Max 5 lines."""

def run_checklist(structure, mtf=None):
    failed = []
    passed = []
    if structure["trend"] == "Ranging":
        failed.append("❌ Trend ranging — no clear direction")
    else:
        passed.append("✅ Trend clear")
    if structure["bos"] == "None":
        failed.append("❌ No BOS detected")
    else:
        passed.append("✅ BOS confirmed")
    if structure["choch"] == "None":
        failed.append("❌ No CHoCH detected")
    else:
        passed.append("✅ CHoCH present")
    zone  = structure["zone"].lower()
    trend = structure["trend"]
    if trend == "Bullish" and "discount" not in zone:
        failed.append("❌ Bullish but price not in Discount")
    elif trend == "Bearish" and "premium" not in zone:
        failed.append("❌ Bearish but price not in Premium")
    else:
        passed.append("✅ Correct zone")
    if not is_trading_session():
        failed.append("❌ Outside London/NY session")
    else:
        passed.append("✅ Session valid")
    if mtf:
        if mtf["verdict"] == "A+":
            passed.append("✅ MTF confluence confirmed")
        elif mtf["verdict"] == "WATCHLIST":
            failed.append(f"❌ MTF not ready: {mtf['reason']}")
        else:
            failed.append(f"❌ MTF conflict: {mtf['reason']}")
    score = len(passed)
    if score == 6:
        rating = "A+"
    elif score >= 4:
        rating = "WATCHLIST"
    else:
        rating = "NO TRADE"
    return {"rating": rating, "score": score, "passed": passed, "failed": failed, "mtf": mtf}

def format_checklist_result(pair, structure, checklist):
    display = pair if pair != "XAUUSD" else "GOLD"
    rating  = checklist["rating"]
    if rating == "A+":
        header = f"🔥 *A+ SETUP — {display}*"
    elif rating == "WATCHLIST":
        header = f"👀 *WATCH THIS — {display}*"
    else:
        header = f"🚫 *NO TRADE — {display}*"
    passed_text = "\n".join(checklist["passed"])
    failed_text = "\n".join(checklist["failed"]) if checklist["failed"] else ""
    return f"""{header}
━━━━━━━━━━━━━━━━━━━━
💰 Price: `{structure['current_price']}`
📊 Trend: {structure['trend']}
📍 Zone: {structure['zone']}
⏰ {get_session_label()}

{passed_text}
{failed_text}

Score: {checklist['score']}/6
"""

def log_trade_to_telegram(pair, structure, checklist):
    now     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    display = pair if pair != "XAUUSD" else "GOLD"
    log_msg = f"""
📓 *TRADE LOG — {display}*
🕐 {now}
💰 Price: `{structure['current_price']}`
📊 Bias: {structure['trend']}
🔍 BOS: {structure['bos']}
⚡ CHoCH: {structure['choch']}
📍 Zone: {structure['zone']}
⏰ {get_session_label()}
🏆 Score: {checklist['score']}/6

#log #{pair} #{get_session_label().split()[0]}
"""
    send_telegram(CHAT_ID, log_msg)

def calculate_trade_levels(structure):
    price  = structure["current_price"]
    s_high = structure["recent_high"]
    s_low  = structure["recent_low"]
    trend  = structure["trend"]
    buffer = round(price * 0.0005, 5)
    if trend == "Bullish":
        entry     = price
        sl        = round(s_low - buffer, 5)
        risk      = round(entry - sl, 5)
        if risk <= 0:
            return None
        tp        = round(entry + (risk * 3), 5)
        reward    = round(tp - entry, 5)
        proximity = abs(price - s_low) / price
        order_type = "LIMIT — approaching discount" if proximity < 0.002 else "MARKET — price at zone"
        exec_note  = "Wait for 15M CHoCH before entering"
    elif trend == "Bearish":
        entry     = price
        sl        = round(s_high + buffer, 5)
        risk      = round(sl - entry, 5)
        if risk <= 0:
            return None
        tp        = round(entry - (risk * 3), 5)
        reward    = round(entry - tp, 5)
        proximity = abs(price - s_high) / price
        order_type = "LIMIT — approaching premium" if proximity < 0.002 else "MARKET — price at zone"
        exec_note  = "Wait for 15M CHoCH before entering"
    else:
        return None
    if reward <= 0:
        return None
    rr = round(reward / risk, 2)
    return {"entry": entry, "sl": sl, "tp": tp, "rr": rr, "risk_pips": round(risk * 10000, 1), "reward_pips": round(reward * 10000, 1), "order_type": order_type, "exec_note": exec_note}

def format_trade_signal(pair, structure, levels):
    display = pair if pair != "XAUUSD" else "GOLD"
    emoji   = "🟢 BUY" if structure["trend"] == "Bullish" else "🔴 SELL"
    return f"""
⚔️ *A+ SIGNAL — {display}*
━━━━━━━━━━━━━━━━━━━━
{emoji}
💰 Entry: `{levels['entry']}`
🛡 SL:    `{levels['sl']}`
🎯 TP:    `{levels['tp']}`
📐 RR:    `1:{levels['rr']}` _(min 3R enforced)_

📋 *Execution:*
• {levels['order_type']}
• Timeframe: 15M
• {levels['exec_note']}

📊 *Structure:*
• Trend: {structure['trend']}
• BOS: {structure['bos']}
• Zone: {structure['zone']}
• {get_session_label()}
━━━━━━━━━━━━━━━━━━━━
⚠️ _Confirm 15M CHoCH on TradingView before entry._
"""

def dashboard_message():
    now    = datetime.now(timezone.utc).strftime("%H:%M UTC")
    status = "🟢 MARKET ACTIVE" if is_trading_session() else "🔴 MARKET CLOSED"
    return f"""
🏦 *JP TRADING DASHBOARD*
━━━━━━━━━━━━━━━━━━━━
📡 {status}
⏰ {get_session_label()} | {now}
🔜 {get_next_session()}
━━━━━━━━━━━━━━━━━━━━
*Pairs:* EURUSD • USDJPY • GBPUSD • GOLD
*System:* SMC/ICT | 3-TF Confluence | 3R Min
━━━━━━━━━━━━━━━━━━━━
Tap a pair for full MTF analysis
or 📡 Scan All for a full sweep.
"""

def rules_message():
    return """
⚔️ *JP ENTRY RULES — 4 CONDITIONS*
━━━━━━━━━━━━━━━━━━━━
All 4 must be true before entry:

*1.* 4H/Daily Break of Structure ✅
*2.* Price tapping 4H Bearish Order Block ✅
*3.* Premium Zone — above 0.618 Fib OTE ✅
*4.* Lower TF CHoCH as entry trigger ✅
━━━━━━━━━━━━━━━━━━━━
⚠️ *If ANY condition is missing → NO TRADE*
Risk: 0.5% per trade | Min RR: 1:3
Sessions: London & New York only
"""

def auto_market_scan():
    if not is_trading_session():
        return
    pairs = ["EURUSD", "USDJPY", "GBPUSD", "XAUUSD"]
    for pair in pairs:
        candles = get_candles(pair, interval="4h", outputsize=20)
        if not candles:
            continue
        structure = detect_structure(candles)
        if not structure:
            continue
        mtf       = multi_timeframe_confluence(pair)
        checklist = run_checklist(structure, mtf)
        if checklist["rating"] == "NO TRADE":
            continue
        prompt = f"""Institutional SMC analyst. Strict.
{pair} 4H: Trend={structure['trend']}, Price={structure['current_price']}, BOS={structure['bos']}, Zone={structure['zone']}, Session={get_session_label()}
ALERT: [Bias] | Zone: [level] | Reason: [one line] — or just CLEAR. No extra text."""
        result = ask_groq(prompt)
        if result and result.strip().upper().startswith("ALERT"):
            if checklist["rating"] == "A+":
                log_trade_to_telegram(pair, structure, checklist)
                levels = calculate_trade_levels(structure)
                if levels:
                    send_telegram(CHAT_ID, format_trade_signal(pair, structure, levels))
            else:
                display = pair if pair != "XAUUSD" else "GOLD"
                send_telegram(CHAT_ID, f"👀 *WATCHLIST — {display}*\n{structure['trend']} | {structure['zone']}\n_{result}_")

@app.route("/")
def home():
    auto_market_scan()
    return "ok", 200

@app.route("/startup")
def startup():
    send_telegram(CHAT_ID, dashboard_message(), main_menu())
    return "ok", 200

@app.route("/testai")
def test_ai():
    result = ask_groq("Say hello in one word.")
    return result if result else "FAILED"

@app.route("/testcandles")
def test_candles():
    candles = get_candles("EURUSD", interval="4h", outputsize=10)
    if not candles:
        return "FAILED - Check TWELVE_DATA_KEY", 500
    return str(detect_structure(candles)), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if "callback_query" in data:
        cb      = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        action  = cb["data"]
        answer_callback(cb["id"])

        if action == "dashboard":
            send_telegram(chat_id, dashboard_message(), main_menu())

        elif action == "session_info":
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")
            send_telegram(chat_id, f"""
⏰ *SESSION STATUS*
━━━━━━━━━━━━━━━━━━━━
Current: {get_session_label()}
Time: {now}
Status: {"🟢 Active — trade your system" if is_trading_session() else "🔴 Closed — wait for London or NY"}
Next: {get_next_session()}
━━━━━━━━━━━━━━━━━━━━
_London: 07:00–12:00 UTC_
_New York: 12:00–17:00 UTC_
""", back_menu())

        elif action == "rules":
            send_telegram(chat_id, rules_message(), back_menu())

        elif action == "checklist":
            send_telegram(chat_id, """
✅ *A+ CHECKLIST*
━━━━━━━━━━━━━━━━━━━━
*1.* 4H Trend clear?
*2.* BOS confirmed?
*3.* Imbalance present?
*4.* OB tapped?
*5.* Session valid? (London/NY only)
*6.* MTF confluence? (Daily + 4H + 1H agree)
*7.* Price in Discount (buys) or Premium (sells)?
━━━━━━━━━━━━━━━━━━━━
⚠️ *If ANY is NO → DON'T TRADE*
""", back_menu())

        elif action == "history":
            send_telegram(chat_id, """
📓 *TRADE LOG*
━━━━━━━━━━━━━━━━━━━━
Your logs are saved above in this chat.

Search by hashtag:
• All logs: #log
• By pair: #EURUSD #GBPUSD #USDJPY #XAUUSD
• By session: #London #New
━━━━━━━━━━━━━━━━━━━━
_Every A+ signal is auto-logged._
""", back_menu())

        elif action == "scan_all":
            send_telegram(chat_id, "📡 *Scanning all pairs...*\n_Running MTF analysis. This may take 20–30 seconds._")
            pairs   = ["EURUSD", "USDJPY", "GBPUSD", "XAUUSD"]
            results = []
            for pair in pairs:
                display = pair if pair != "XAUUSD" else "GOLD"
                candles = get_candles(pair, interval="4h", outputsize=20)
                if not candles:
                    results.append(f"⚠️ {display}: Data unavailable")
                    continue
                structure = detect_structure(candles)
                if not structure:
                    results.append(f"⚠️ {display}: Structure failed")
                    continue
                mtf       = multi_timeframe_confluence(pair)
                checklist = run_checklist(structure, mtf)
                rating    = checklist["rating"]
                if rating == "A+":
                    results.append(f"🔥 {display}: A+ SETUP — {structure['trend']} | {checklist['score']}/6")
                elif rating == "WATCHLIST":
                    results.append(f"👀 {display}: WATCHLIST — {structure['trend']} | {checklist['score']}/6")
                else:
                    results.append(f"🚫 {display}: NO TRADE | {checklist['score']}/6")
            summary = "\n".join(results)
            send_telegram(chat_id, f"""
📡 *FULL MARKET SCAN*
━━━━━━━━━━━━━━━━━━━━
{summary}
━━━━━━━━━━━━━━━━━━━━
⏰ {get_session_label()} | {datetime.now(timezone.utc).strftime("%H:%M UTC")}
""", main_menu())

        elif action.startswith("price_"):
            pair    = action.replace("price_", "")
            display = pair if pair != "XAUUSD" else "GOLD"
            send_telegram(chat_id, f"📊 *{display}* — Running MTF analysis...", main_menu())
            candles = get_candles(pair, interval="4h", outputsize=20)
            if candles:
                structure = detect_structure(candles)
                if structure:
                    mtf           = multi_timeframe_confluence(pair)
                    checklist     = run_checklist(structure, mtf)
                    checklist_msg = format_checklist_result(pair, structure, checklist)
                    send_telegram(chat_id, checklist_msg, main_menu())
                    if checklist["rating"] == "A+":
                        log_trade_to_telegram(pair, structure, checklist)
                        confirmed, ltf_reason = confirm_entry_15m(pair)
                        if confirmed:
                            levels = calculate_trade_levels(structure)
                            if levels:
                                send_telegram(chat_id, format_trade_signal(pair, structure, levels), main_menu())
                        else:
                            send_telegram(chat_id, f"""
⏳ *A+ SETUP — WAITING FOR ENTRY*

*{display}* structure is ready but 15M not confirmed yet.

🔍 LTF Status: {ltf_reason}

_Wait for 15M CHoCH on TradingView, then re-tap the pair._
""", main_menu())
                    elif checklist["rating"] == "WATCHLIST":
                        send_telegram(chat_id, f"👀 *{display}* setting up but not ready. Monitor for 1H entry zone.", main_menu())
                    structure_summary = f"""
📊 *{display}* — 4H Structure
━━━━━━━━━━━━━━━━━━━━
💰 Price: `{structure['current_price']}`
📈 Trend: {structure['trend']}
🔺 Swing High: `{structure['recent_high']}`
🔻 Swing Low:  `{structure['recent_low']}`
🔍 BOS: {structure['bos']}
⚡ CHoCH: {structure['choch']}
📍 Zone: {structure['zone']}
⏰ {get_session_label()}
"""
                    send_telegram(chat_id, structure_summary, main_menu())
                    prompt   = build_smc_prompt(pair, structure)
                    analysis = ask_groq(prompt)
                    if analysis:
                        send_telegram(chat_id, f"🧠 *SMC READ — {display}*\n\n{analysis}", main_menu())
                else:
                    send_telegram(chat_id, "⚠️ Could not detect structure from candles.", main_menu())
            else:
                rate = get_forex_price(pair)
                if rate:
                    send_telegram(chat_id, f"⚠️ Candle data unavailable. Spot price: `{rate}`", main_menu())
                else:
                    send_telegram(chat_id, "⚠️ Could not fetch price. Try again.", main_menu())

    if "message" in data:
        msg     = data["message"]
        chat_id = msg["chat"]["id"]

        if "photo" in msg:
            send_telegram(chat_id, "📸 Chart received. Running SMC analysis...")
            file_id = msg["photo"][-1]["file_id"]
            try:
                img_b64  = get_file_base64(file_id)
                analysis = ask_groq_image(img_b64)
                send_telegram(chat_id, f"🧠 *SMC CHART READ*\n\n{analysis}", main_menu())
            except Exception as e:
                send_telegram(chat_id, f"⚠️ Error: {str(e)}", main_menu())

        elif "text" in msg:
            text = msg["text"]
            if text in ["/start", "/menu"]:
                send_telegram(chat_id, dashboard_message(), main_menu())
            elif text == "/check":
                send_telegram(chat_id, """
✅ *A+ CHECKLIST*
━━━━━━━━━━━━━━━━━━━━
*1.* 4H Trend clear?
*2.* BOS confirmed?
*3.* Imbalance present?
*4.* OB tapped?
*5.* Session valid?
*6.* MTF confluence?
*7.* Correct zone?
━━━━━━━━━━━━━━━━━━━━
⚠️ If ANY is NO → DON'T TRADE
""", main_menu())
            elif text == "/history":
                send_telegram(chat_id, "📓 Search #log in this chat. Filter: #EURUSD #London etc.", main_menu())
            elif text == "/rules":
                send_telegram(chat_id, rules_message(), main_menu())
            elif text == "/scan":
                send_telegram(chat_id, "📡 Manual scan triggered...", main_menu())
                auto_market_scan()
            else:
                reply = ask_groq_chat(text)
                if reply:
                    send_telegram(chat_id, reply, main_menu())
                else:
                    send_telegram(chat_id, "🤖 I'm here. Tap a pair or use /menu.", main_menu())

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
