from flask import Flask, request
import requests
import os

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

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
            send_telegram(chat_id, f"⏳ Fetching {pair} price...", main_menu())

    # Handle text commands
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]

        if "text" in msg:
            text = msg["text"]

            if text == "/start" or text == "/menu":
                send_telegram(chat_id, "👋 Welcome to *JP Trading Dashboard*\n\nChoose an option below:", main_menu())

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
