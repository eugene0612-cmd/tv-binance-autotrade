import os
import math
from flask import Flask, request, jsonify, abort
from pybit.unified_trading import HTTP

# ----------------------
# 환경변수
# ----------------------
API_KEY       = os.getenv("BYBIT_API_KEY", "")
API_SECRET    = os.getenv("BYBIT_API_SECRET", "")
WEBHOOK_SECRET= os.getenv("WEBHOOK_SECRET", "")
USE_TESTNET   = os.getenv("USE_TESTNET", "true").lower() == "true"
SYMBOL        = os.getenv("SYMBOL", "BTCUSDT")        # Bybit 선물 심볼
LEVERAGE      = int(os.getenv("LEVERAGE", "5"))       # 레버리지
POSITION_USDT = float(os.getenv("POSITION_USDT", "50")) # 1회 진입 금액(USDT)

app = Flask(__name__)

# Bybit 세션
session = HTTP(testnet=USE_TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ----------------------
# 유틸
# ----------------------
def get_mark_price(symbol: str) -> float:
    r = session.get_tickers(category="linear", symbol=symbol)
    return float(r["result"]["list"][0]["lastPrice"])

def get_lot_filters(symbol: str):
    """거래가능 최소/스텝 조회(수량 반올림용)"""
    r = session.get_instruments_info(category="linear", symbol=symbol)
    info = r["result"]["list"][0]
    f = info["lotSizeFilter"]
    min_qty = float(f["minOrderQty"])
    qty_step = float(f["qtyStep"])
    return min_qty, qty_step

def round_qty(qty: float, step: float, min_qty: float) -> float:
    if qty < min_qty:
        return 0.0
    return math.floor(qty / step) * step

def set_oneway_and_leverage(symbol: str, leverage: int):
    """원웨이 모드 + 레버리지 설정(가능한 경우)"""
    try:
        # 포지션 모드(원웨이)
        session.switch_position_mode(category="linear", symbol=symbol, mode=0)  # 0: Oneway, 3: Hedge
    except Exception as e:
        print("switch_position_mode error:", e)
    try:
        session.set_leverage(category="linear", symbol=symbol, buyLeverage=str(leverage), sellLeverage=str(leverage))
    except Exception as e:
        print("set_leverage error:", e)

def get_position_amt(symbol: str) -> float:
    """현재 포지션 수량(+)롱/(-)숏, 없으면 0"""
    r = session.get_positions(category="linear", symbol=symbol)
    if not r["result"]["list"]:
        return 0.0
    p = r["result"]["list"][0]
    side = p["side"]           # "Buy" or "Sell"
    size = float(p["size"]) if p["size"] else 0.0
    return size if side == "Buy" else -size

def market_close(side: str, qty: float):
    """reduceOnly 시장가 청산"""
    if qty <= 0:
        return
    try:
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,                 # 청산하려는 반대사이드로
            orderType="Market",
            qty=qty,
            reduceOnly=True,
            timeInForce="GoodTillCancel"
        )
        print(f"[CLOSE] {side} {qty}")
    except Exception as e:
        print("market_close error:", e)

def market_open(side: str, qty: float):
    try:
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,                 # "Buy" 롱 / "Sell" 숏
            orderType="Market",
            qty=qty,
            reduceOnly=False,
            timeInForce="GoodTillCancel"
        )
        print(f"[OPEN] {side} {qty}")
    except Exception as e:
        print("market_open error:", e)

# ----------------------
# 웹훅 엔드포인트
# ----------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    # 1) 보안확인
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        abort(401, "Bad secret")

    signal = (data.get("signal") or "").upper()
    if signal not in ("BUY", "SELL"):
        abort(400, "Signal must be BUY or SELL")

    # 2) 모드/레버리지 설정 시도
    set_oneway_and_leverage(SYMBOL, LEVERAGE)

    # 3) 수량 계산 (USDT 금액 → 수량)
    price = get_mark_price(SYMBOL)
    min_qty, step = get_lot_filters(SYMBOL)
    raw_qty = POSITION_USDT / price
    qty = round_qty(raw_qty, step, min_qty)
    if qty <= 0:
        abort(400, f"Qty too small for {SYMBOL}. Increase POSITION_USDT.")

    # 4) 반대 포지션 청산
    cur = get_position_amt(SYMBOL)  # +롱 / -숏 / 0
    if signal == "BUY" and cur < 0:
        market_close("Buy", abs(cur))     # 숏 청산 = Buy reduceOnly
    if signal == "SELL" and cur > 0:
        market_close("Sell", cur)         # 롱 청산 = Sell reduceOnly

    # 5) 동일방향 중복 진입 방지
    cur = get_position_amt(SYMBOL)
    if signal == "BUY" and cur > 0:
        return jsonify({"status": "ok", "note": "already long"})
    if signal == "SELL" and cur < 0:
        return jsonify({"status": "ok", "note": "already short"})

    # 6) 신규 진입
    side = "Buy" if signal == "BUY" else "Sell"
    market_open(side, qty)

    return jsonify({"status": "ok", "executed": signal, "qty": qty, "price": price}), 200

@app.route("/", methods=["GET"])
def health():
    return "OK (Bybit)", 200
