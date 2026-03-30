from flask import Flask, request
import requests
import os

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": text
    })

@app.route("/")
def home():
    # Ping this route = bot messages you automatically
    send_telegram(CHAT_ID, "✅ JP mini bot is live and running!")
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]

        if "photo" in msg:
            caption = msg.get("caption", "No caption")
            response = f"""
📊 SETUP LOGGED

🧾 {caption}

🧠 Checklist:
✅ Trend
✅ BOS
✅ Imbalance
✅ OB Tap

⚠️ Stay disciplined.
"""
            send_telegram(chat_id, response)

        elif "text" in msg:
            if msg["text"] == "/check":
                checklist = """
🧠 A+ CHECKLIST

✅ 1H Trend clear?
✅ BOS confirmed?
✅ Imbalance present?
✅ OB tapped?
✅ Session valid?
✅ News nearby?

⚠️ If ANY is NO → DON'T TRADE
"""
                send_telegram(chat_id, checklist)

            elif msg["text"] == "/start":
                send_telegram(chat_id, "👋 JP mini bot is ready. Send /check for your A+ checklist.")

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
