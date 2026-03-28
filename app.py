from flask import Flask, request
import requests

app = Flask(__name__)

BOT_TOKEN = "8763117864:AAEkKkFXvuLSHDVDqyRY3a4E5Jlb5VtkA4A"
CHAT_ID = 7189582757

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    pair = data.get("pair", "Unknown")
    signal = data.get("signal", "No signal")
    price = data.get("price", "N/A")

    msg = f"📊 {pair}\n⚡ {signal}\n💰 {price}"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": msg
    })

    return "OK"
