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
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY")  # NEW: add this to Render env vars

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────

def send_telegram(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload)

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id})

def main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🇪🇺 EURUSD", "callback_data": "price_EURUSD"},
                {"text": "🇯🇵 USDJPY", "callback_data": "price_USDJPY"}
            ],
            [
                {"text": "🇬🇧 GBPUSD", "callback_data": "price_GBPUSD"},
                {"text": "🥇 GOLD", "callback_data": "price_XAUUSD"}
            ],
            [
                {"text": "✅ A+ Checklist", "callback_data": "checklist"}
            ]
        ]
    }

# ─────────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────────

def get_forex_price(pair):
    if pair == "XAUUSD":
        try:
            url = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
            res = requests.get(url, timeout=10).json()
            return round(res[0]["spreadProfilePrices"][0]["ask"], 2)
        except:
            return None

    pair_map = {
        "EURUSD": ("EUR", "USD"),
        "USDJPY": ("USD", "JPY"),
        "GBPUSD": ("GBP", "USD"),
    }
    base, quote = pair_map[pair]
    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/pair/{base}/{quote}"
    try:
        res = requests.get(url, timeout=10).json()
        return res["conversion_rate"]
    except:
        return None

# ─────────────────────────────────────────────
# REAL OHLC CANDLE DATA (NEW)
# ─────────────────────────────────────────────

def get_candles(pair, interval="4h", outputsize=10):
    """
    Fetches real OHLC candle data from Twelve Data.
    interval options: "1h", "4h", "1day"
    outputsize = number of candles to return (max 5000 on free tier)
    Free tier: 8 requests/minute, 800/day — more than enough for our bot.
    Sign up free at: https://twelvedata.com
    """
    # Twelve Data uses slash format e.g. EUR/USD, XAU/USD
    symbol_map = {
        "EURUSD": "EUR/USD",
        "USDJPY": "USD/JPY",
        "GBPUSD": "GBP/USD",
        "XAUUSD": "XAU/USD"
    }
    symbol = symbol_map.get(pair)
    if not symbol:
        return None

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY
    }
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res.get("status") == "error":
            return None
        candles = res.get("values", [])
        # Returns newest first — reverse so oldest is index 0
        candles.reverse()
        return candles
    except:
        return None

# ─────────────────────────────────────────────
# REAL SMC STRUCTURE DETECTION (NEW)
# ─────────────────────────────────────────────

def detect_structure(candles):
    """
    Takes a list of OHLC candles (oldest first) and detects:
    - Trend direction (bullish/bearish/ranging)
    - Recent swing high and swing low
    - BOS (Break of Structure)
    - CHoCH (Change of Character)
    - Whether price is in premium or discount zone
    Returns a dict with all structure data.
    """
    if not candles or len(candles) < 5:
        return None

    # Extract highs, lows, closes as floats
    highs  = [float(c["high"])  for c in candles]
    lows   = [float(c["low"])   for c in candles]
    closes = [float(c["close"]) for c in candles]

    current_price = closes[-1]

    # Recent swing high and low (last 10 candles)
    recent_high = max(highs[-10:])
    recent_low  = min(lows[-10:])

    # Trend: compare last close to close 5 candles ago
    prev_close = closes[-6] if len(closes) >= 6 else closes[0]
    price_change_pct = ((current_price - prev_close) / prev_close) * 100

    if price_change_pct > 0.15:
        trend = "Bullish"
    elif price_change_pct < -0.15:
        trend = "Bearish"
    else:
        trend = "Ranging"

    # BOS detection:
    # Bullish BOS = current close breaks above the recent swing high
    # Bearish BOS = current close breaks below the recent swing low
    mid_high = max(highs[-10:-1]) if len(highs) >= 10 else recent_high
    mid_low  = min(lows[-10:-1])  if len(lows)  >= 10 else recent_low

    bos = "None"
    if current_price > mid_high:
        bos = "Bullish BOS (broke above swing high)"
    elif current_price < mid_low:
        bos = "Bearish BOS (broke below swing low)"

    # CHoCH detection:
    # If we had a bearish trend but price is now making higher lows — potential CHoCH
    # Simplified: check if last 3 lows are rising in a bearish trend or vice versa
    choch = "None"
    if len(lows) >= 3:
        if trend == "Bearish" and lows[-1] > lows[-2] > lows[-3]:
            choch = "Potential Bullish CHoCH (rising lows in downtrend)"
        elif trend == "Bullish" and highs[-1] < highs[-2] < highs[-3]:
            choch = "Potential Bearish CHoCH (falling highs in uptrend)"

    # Premium / Discount Zone
    # Range = recent swing high to recent swing low
    # 0.5 = equilibrium, above = premium, below = discount
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
        "zone": zone,
        "price_change_pct": round(price_change_pct, 3)
    }

# ─────────────────────────────────────────────
# GROQ AI
# ─────────────────────────────────────────────

def ask_groq(prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 300,
        "messages": [
            {
                "role": "system",
                "content": "You are a sharp SMC/ICT forex trading analyst. Keep responses short, smart and actionable. Reject low-quality setups. Only speak if high probability."
            },
            {"role": "user", "content": prompt}
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=25)
        return res.json()["choices"][0]["message"]["content"]
    except:
        return None

def ask_groq_image(base64_image):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "max_tokens": 400,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                            "detail": "low"
                        }
                    },
                    {
                        "type": "text",
                        "text": "You are a sharp SMC/ICT trading analyst. Analyse this chart and give me: 1. Trend bias 2. Key zone to watch 3. Entry condition 4. One risk note. Keep it short and sharp."
                    }
                ]
            }
        ]
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

# ─────────────────────────────────────────────
# IMAGE HELPER
# ─────────────────────────────────────────────

def get_file_base64(file_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    res = requests.get(url).json()
    file_path = res["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    img_data = requests.get(file_url).content
    return base64.b64encode(img_data).decode("utf-8")

# ─────────────────────────────────────────────
# SESSION FILTER
# ─────────────────────────────────────────────

def is_trading_session():
    now = datetime.now(timezone.utc)
    hour = now.hour
    # London: 7AM–12PM UTC, NY: 12PM–5PM UTC
    return (7 <= hour < 12) or (12 <= hour < 17)

def get_session_label():
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour < 12:
        return "London 🇬🇧"
    elif 12 <= hour < 17:
        return "New York 🇺🇸"
    return "Off-Session"

# ─────────────────────────────────────────────
# SMART SMC PROMPT BUILDER (NEW)
# ─────────────────────────────────────────────

def build_smc_prompt(pair, structure):
    """
    Builds a prompt using REAL structure data instead of fake guesses.
    AI is now a refiner, not a guesser.
    """
    return f"""You are a strict institutional SMC/ICT trading analyst.

Here is REAL 4H market structure data for {pair}:

- Current Price: {structure['current_price']}
- Trend: {structure['trend']}
- Recent Swing High: {structure['recent_high']}
- Recent Swing Low: {structure['recent_low']}
- BOS: {structure['bos']}
- CHoCH: {structure['choch']}
- Fib Zone: {structure['zone']}
- Price Change (last 5 candles): {structure['price_change_pct']}%
- Session: {get_session_label()}

Using this data, give me:
1. Bias (Bullish / Bearish / No Trade)
2. Key zone to watch
3. Entry condition (be specific)
4. Risk note

Rules:
- If trend is Ranging and no BOS/CHoCH, say NO TRADE
- If price is in Premium looking for buys, say NO TRADE
- If price is in Discount looking for sells, say NO TRADE
- Maximum 5 lines. Be ruthless."""

def run_checklist(structure):
    """
    Runs the A+ checklist against real structure data.
    Returns a dict with score, rating, and failed conditions.
    """
    failed = []
    passed = []

    # 1. Trend clear?
    if structure["trend"] == "Ranging":
        failed.append("❌ Trend is ranging — no clear direction")
    else:
        passed.append("✅ Trend clear")

    # 2. BOS confirmed?
    if structure["bos"] == "None":
        failed.append("❌ No BOS detected")
    else:
        passed.append("✅ BOS confirmed")

    # 3. CHoCH present?
    if structure["choch"] == "None":
        failed.append("❌ No CHoCH detected")
    else:
        passed.append("✅ CHoCH present")

    # 4. Correct zone?
    zone = structure["zone"].lower()
    trend = structure["trend"]
    if trend == "Bullish" and "discount" not in zone:
        failed.append("❌ Bullish bias but price not in Discount zone")
    elif trend == "Bearish" and "premium" not in zone:
        failed.append("❌ Bearish bias but price not in Premium zone")
    else:
        passed.append("✅ Price in correct zone")

    # 5. Session valid?
    if not is_trading_session():
        failed.append("❌ Outside London/NY session")
    else:
        passed.append("✅ Session valid")

    score = len(passed)

    if score == 5:
        rating = "A+"
    elif score >= 3:
        rating = "WATCHLIST"
    else:
        rating = "NO TRADE"

    return {
        "rating": rating,
        "score": score,
        "passed": passed,
        "failed": failed
    }


def format_checklist_result(pair, structure, checklist):
    """
    Formats the checklist result into a clean Telegram message.
    """
    display = pair if pair != "XAUUSD" else "GOLD"
    rating = checklist["rating"]

    if rating == "A+":
        header = f"🔥 *A+ SETUP — {display}*"
    elif rating == "WATCHLIST":
        header = f"👀 *WATCH THIS — {display}*"
    else:
        header = f"🚫 *NO TRADE — {display}*"

    passed_text = "\n".join(checklist["passed"])
    failed_text = "\n".join(checklist["failed"]) if checklist["failed"] else ""

    msg = f"""{header}

💰 Price: `{structure['current_price']}`
📊 Trend: {structure['trend']}
📍 Zone: {structure['zone']}
⏰ Session: {get_session_label()}

{passed_text}
{failed_text}

Score: {checklist['score']}/5
"""
    return msg
    
# ─────────────────────────────────────────────
# AUTO SCAN (UPGRADED)
# ─────────────────────────────────────────────

def auto_market_scan():
    if not is_trading_session():
        return

    pairs = ["EURUSD", "USDJPY", "GBPUSD", "XAUUSD"]
    for pair in pairs:
        # Get real candle data
        candles = get_candles(pair, interval="4h", outputsize=20)
        if not candles:
            continue

        structure = detect_structure(candles)
        checklist = run_checklist(structure)
        if checklist["rating"] == "NO TRADE":
            continue
        if not structure:
            continue

        prompt = f"""You are an institutional SMC/ICT trading analyst. Be extremely strict.

Real 4H structure for {pair}:
- Trend: {structure['trend']}
- Price: {structure['current_price']}
- Swing High: {structure['recent_high']} | Swing Low: {structure['recent_low']}
- BOS: {structure['bos']}
- CHoCH: {structure['choch']}
- Zone: {structure['zone']}
- Session: {get_session_label()}

Only say ALERT if:
- Trend is clear (not ranging)
- BOS or CHoCH is confirmed
- Price is at a key zone (discount for buys, premium for sells)
- Session aligns

Format EXACTLY like this if alerting:
ALERT: [Bias] | Zone: [level] | Reason: [one line max]

Or just:
CLEAR

No extra text. Be ruthless."""

        result = ask_groq(prompt)

        if result and result.strip().upper().startswith("ALERT"):
            send_telegram(CHAT_ID, f"""
🏦 *INSTITUTIONAL ALERT — {pair}*

💰 Price: `{structure['current_price']}`
📊 Trend: {structure['trend']}
🔍 BOS: {structure['bos']}
⚡ CHoCH: {structure['choch']}
📍 Zone: {structure['zone']}

{result}

⏰ Session: {get_session_label()}
""")

# ─────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def home():
    auto_market_scan()
    return "ok", 200

@app.route("/startup")
def startup():
    send_telegram(CHAT_ID, "✅ JP mini bot is live and running!")
    return "ok", 200

@app.route("/testai")
def test_ai():
    result = ask_groq("Say hello in one word.")
    return result if result else "FAILED - AI unavailable"

@app.route("/testcandles")
def test_candles():
    """Test route — check if OHLC data is working"""
    candles = get_candles("EURUSD", interval="4h", outputsize=10)
    if not candles:
        return "FAILED - No candle data. Check TWELVE_DATA_KEY env var.", 500
    structure = detect_structure(candles)
    return str(structure), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    # Handle button taps
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        action = cb["data"]
        answer_callback(cb["id"])

        if action == "checklist":
            send_telegram(chat_id, """
🧠 *A+ CHECKLIST*

✅ 4H Trend clear?
✅ BOS confirmed?
✅ Imbalance present?
✅ OB tapped?
✅ Session valid?
✅ News nearby?
✅ Price in Discount (for buys) or Premium (for sells)?

⚠️ If ANY is NO → DON'T TRADE
""", main_menu())

        elif action.startswith("price_"):
            pair = action.replace("price_", "")
            display = pair if pair != "XAUUSD" else "GOLD"

            # Send immediate response
            send_telegram(chat_id, f"📊 *{display}* — Fetching real structure data...", main_menu())

            # Get real candle data
            candles = get_candles(pair, interval="4h", outputsize=20)

            if candles:
                structure = detect_structure(candles)
                if structure:
                    # Build prompt with REAL data
                    prompt = build_smc_prompt(pair, structure)
                    checklist = run_checklist(structure)
                    checklist_msg = format_checklist_result(pair, structure, checklist)
                    send_telegram(chat_id, checklist_msg, main_menu())

                    if checklist["rating"] == "NO TRADE":
                        pass
                    elif checklist["rating"] == "WATCHLIST":
                        send_telegram(chat_id, f"👀 *{display}* is setting up but not ready yet. Monitor this pair.", main_menu())
                    analysis = ask_groq(prompt)

                    structure_summary = f"""
📊 *{display}* — Real 4H Structure

💰 Price: `{structure['current_price']}`
📈 Trend: {structure['trend']}
🔺 Swing High: `{structure['recent_high']}`
🔻 Swing Low: `{structure['recent_low']}`
🔍 BOS: {structure['bos']}
⚡ CHoCH: {structure['choch']}
📍 Zone: {structure['zone']}
⏰ Session: {get_session_label()}
"""
                    send_telegram(chat_id, structure_summary, main_menu())

                    if analysis:
                        send_telegram(chat_id, f"🧠 *SMC READ — {display}*\n\n{analysis}", main_menu())
                else:
                    send_telegram(chat_id, "⚠️ Could not detect structure from candles.", main_menu())
            else:
                # Fallback to spot price only
                rate = get_forex_price(pair)
                if rate:
                    send_telegram(chat_id, f"⚠️ Candle data unavailable. Spot price: `{rate}`\n\n_Add TWELVE\\_DATA\\_KEY to Render env vars for full analysis._", main_menu())
                else:
                    send_telegram(chat_id, "⚠️ Could not fetch price. Try again.", main_menu())

    # Handle messages
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]

        # Chart screenshot analysis
        if "photo" in msg:
            send_telegram(chat_id, "📸 Chart received. Running SMC analysis...")
            file_id = msg["photo"][-1]["file_id"]
            try:
                img_b64 = get_file_base64(file_id)
                analysis = ask_groq_image(img_b64)
                send_telegram(chat_id, f"🧠 *SMC CHART READ*\n\n{analysis}", main_menu())
            except Exception as e:
                send_telegram(chat_id, f"⚠️ Error: {str(e)}", main_menu())

        elif "text" in msg:
            text = msg["text"]

            if text == "/start" or text == "/menu":
                send_telegram(chat_id, "👋 Welcome to *JP Trading Dashboard*\n\nTap a pair for live price + real SMC structure, or send a chart screenshot for AI analysis:", main_menu())

            elif text == "/check":
                send_telegram(chat_id, """
🧠 *A+ CHECKLIST*

✅ 4H Trend clear?
✅ BOS confirmed?
✅ Imbalance present?
✅ OB tapped?
✅ Session valid?
✅ News nearby?
✅ Price in Discount (buys) or Premium (sells)?

⚠️ If ANY is NO → DON'T TRADE
""", main_menu())

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
