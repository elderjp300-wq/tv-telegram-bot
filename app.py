from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
import requests
import os
import base64

app = Flask(__name__):
scheduler = BackgroundScheduler()
scheduler.add_job(auto_market_scan, 'interval', minutes=30)
scheduler.start()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
EXCHANGE_API_KEY = os.environ.get("EXCHANGE_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

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

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id})

def get_forex_price(pair):
    pair_map = {
        "EURUSD": ("EUR", "USD"),
        "USDJPY": ("USD", "JPY"),
        "GBPUSD": ("GBP", "USD"),
        "XAUUSD": ("XAU", "USD")
    }
    base, quote = pair_map[pair]
    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/pair/{base}/{quote}"
    try:
        res = requests.get(url).json()
        return res["conversion_rate"]
    except:
        return None

def ask_groq(prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.environ.get('GROQ_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": "You are a sharp SMC/ICT forex trading analyst. Keep responses short, smart and actionable."},
            {"role": "user", "content": prompt}
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload)
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

def get_file_base64(file_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    res = requests.get(url).json()
    file_path = res["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    img_data = requests.get(file_url).content
    return base64.b64encode(img_data).decode("utf-8")

def auto_market_scan():
    pairs = ["EURUSD", "USDJPY", "GBPUSD"]
    for pair in pairs:
        rate = get_forex_price(pair)
        if not rate:
            continue

        prompt = f"""You are a sharp SMC/ICT trading analyst.
Current {pair} price is {rate}.

Scan for opportunity. Reply with ONLY one of:
- "ALERT: [your short SMC reason]" if there is a valid setup forming
- "CLEAR" if nothing significant

Be strict. Only alert if price is at a key SMC zone."""

        result = ask_groq(prompt)

        if result and "ALERT" in result.upper():
            send_telegram(CHAT_ID, f"""
🚨 *MARKET ALERT — {pair}*

💰 Price: `{rate}`

{result}
""")

@app.route("/")
def home():
    return "ok", 200

@app.route("/startup")
def startup():
    send_telegram(CHAT_ID, "✅ JP mini bot is live and running!")
    return "ok", 200

@app.route("/testai")
def test_ai():
    result = ask_groq("Say hello in one word.")
    return result if result else "FAILED - AI unavailable"
    
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

✅ 1H Trend clear?
✅ BOS confirmed?
✅ Imbalance present?
✅ OB tapped?
✅ Session valid?
✅ News nearby?

⚠️ If ANY is NO → DON'T TRADE
""", main_menu())

        elif action.startswith("price_"):
            pair = action.replace("price_", "")
            display = pair if pair != "XAUUSD" else "GOLD"
            rate = get_forex_price(pair)

            if rate:
                # Send price immediately
                send_telegram(chat_id, f"""
        📊 *{display}* — `{rate}`

        🧠 Running SMC read...
        """, main_menu())

                # Then run AI separately
                prompt = f"""You are a sharp SMC/ICT trading analyst.
        Current {display} price is {rate}.

        Give me:
        1. Bias (bullish/bearish/ranging)
        2. Key zone to watch
        3. Entry condition
        4. One risk note
        
        5 lines max. Sharp and smart."""

                analysis = ask_groq(prompt)

                if analysis:
                    send_telegram(chat_id, f"🧠 *SMC READ — {display}*\n\n{analysis}", main_menu())
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
                send_telegram(chat_id, "👋 Welcome to *JP Trading Dashboard*\n\nTap a pair for live price + SMC read, or send a chart screenshot for AI analysis:", main_menu())

            elif text == "/check":
                send_telegram(chat_id, """
🧠 *A+ CHECKLIST*

✅ 1H Trend clear?
✅ BOS confirmed?
✅ Imbalance present?
✅ OB tapped?
✅ Session valid?
✅ News nearby?

⚠️ If ANY is NO → DON'T TRADE
""", main_menu())

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
