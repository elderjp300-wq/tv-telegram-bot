from flask import Flask, request
import requests
import os

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if "message" in data:
        msg = data["message"]

        # 📸 Image handler
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
            send_telegram(response)

        # 🧠 Checklist command
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
                send_telegram(checklist)

    return "ok"
