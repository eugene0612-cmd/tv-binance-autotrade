import os
from flask import Flask, request, jsonify, abort
from pybit.unified_trading import HTTP

app = Flask(__name__)

# 환경변수에서 키 불러오기
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

session = HTTP(
    testnet=USE_TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET
)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if data.get("secret") != WEBHOOK_SECRET:
        return abort(401, "Bad secret")

    signal = data.get("signal")
    if signal not in ("BUY", "SELL"):
        return abort(400, "Signal must be BUY or SELL")

    # 포지션 조회
    positions = session.get_positions(category="linear", symbol=SYMBOL)
    side = "Buy" if signal == "BUY" else "Sell"

    # 반대 포지션 청산
    if signal == "BUY":
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side="Buy",
            orderType="Market",
            qty=0.01,
            reduceOnly=False
        )
    elif signal == "SELL":
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side="Sell",
            orderType="Market",
            qty=0.01,
            reduceOnly=False
        )

    return jsonify({"status": "ok", "executed": signal})

@app.route("/")
def health():
    return "OK (Bybit)"
