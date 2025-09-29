from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)

# 환경변수에서 키 불러오기
import os
API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_API_SECRET")

# Bybit 세션
session = HTTP(
    testnet=False,   # True면 테스트넷
    api_key=API_KEY,
    api_secret=API_SECRET
)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    signal = data.get("signal")

    try:
        if signal == "BUY":
            # 숏 포지션 있으면 청산, 롱 진입
            session.place_order(
                category="linear",
                symbol="BTCUSDT",
                side="Buy",
                orderType="Market",
                qty=0.01
            )
        elif signal == "SELL":
            # 롱 포지션 있으면 청산, 숏 진입
            session.place_order(
                category="linear",
                symbol="BTCUSDT",
                side="Sell",
                orderType="Market",
                qty=0.01
            )
        return jsonify({"status": "ok", "signal": signal})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route("/")
def home():
    return "Bybit bot running!"
