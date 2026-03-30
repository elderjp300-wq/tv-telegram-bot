from flask import Flask, request
import requests
import os
import base64

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
EXCHANGE_API_KEY = os.environ.get("EXCHANGE_API_KEY")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")

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

def ask_claude(prompt):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload)
        return res.json()["content"][0]["text"]
    except:
        return None

def ask_claude_image(base64_image, pair_context="forex"):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64_image
                        }
                    },
                    {
                        "type": "text",
                        "text": """You are a sharp SMC/ICT trading analyst. Analyse this chart and give a short, smart breakdown:

1. Trend bias (bullish/bearish/ranging)
2. Key zone to watch (OB, FVG, or liquidity level)
3. What price needs to do to confirm entry
4. One line risk note

Keep it short, sharp and SMC-flavoured. No fluff."""
                    }
                ]
            }
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload)
        return res.json()["content"][0]["text"]
    except:
        return None

def get_file_base64(file_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    res = requests.get(url).json()
    file_path = res["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    img_data = requests.get(file_url).content
    return base64.b64encode(img_data).decode("utf-8")

@app.route("/")
def home():
    return "ok", 200

@app.route("/startup")
def startup():
    send_telegram(CHAT_ID, "✅ JP mini bot is live and running!")
    return "ok", 200

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
                send_telegram(chat_id, f"⏳ Got price. Running SMC read on {display}...", main_menu())

                prompt = f"""You are a sharp SMC/ICT trading analyst. 
Current {display} price is {rate}.

Give me a short smart SMC-style read:
1. Quick bias (bullish/bearish/ranging)
2. Key zone to watch right now
3. What to look for before entering
4. One risk note

Keep it sharp, 5 lines max. No fluff."""

                analysis = ask_claude(prompt)

                if analysis:
                    send_telegram(chat_id, f"""
📊 *{display}* — `{rate}`

{analysis}
""", main_menu())
                else:
                    send_telegram(chat_id, f"📊 *{display}*\n\n💰 Price: `{rate}`\n\n⚠️ AI read unavailable.", main_menu())
            else:
                send_telegram(chat_id, "⚠️ Could not fetch price. Try again.", main_menu())

    # Handle messages
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]

        # Chart screenshot analysis
        if "photo" in msg:
            send_telegram(chat_id, "📸 Chart received. Running SMC analysis...", main_menu())
            file_id = msg["photo"][-1]["file_id"]
            try:
                img_b64 = get_file_base64(file_id)
                analysis = ask_claude_image(img_b64)
                if analysis:
                    send_telegram(chat_id, f"🧠 *SMC CHART READ*\n\n{analysis}", main_menu())
                else:
                    send_telegram(chat_id, "⚠️ Could not analyse chart. Try again.", main_menu())
            except:
                send_telegram(chat_id, "⚠️ Error reading chart. Try again.", main_menu())

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
