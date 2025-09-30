import os
import math
import time
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)

# === 환경 변수 ===
API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
USE_TESTNET = os.environ.get("USE_TESTNET", "true").lower() == "true"

SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
LEVERAGE = int(os.environ.get("LEVERAGE", "5"))
POSITION_USDT = float(os.environ.get("POSITION_USDT", "50"))

# === Bybit HTTP 클라이언트 (V5 / Unified Trading) ===
session = HTTP(
    testnet=USE_TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET,
    # 옵션: timeout=15, recv_window=5000 등 필요 시 추가
)

CATEGORY = "linear"     # USDT Perp
POSITION_IDX = 0        # One-way 모드 (Hedge 모드면 1/2 사용)

# === 유틸 ===
def _resp(status: int, ok=True, **kw):
    return jsonify({"ok": ok, "status": status, **kw}), status

def get_symbol_info(symbol: str):
    """수량 스텝/최소수량 등을 얻기 위한 상품 정보"""
    res = session.get_instruments_info(category=CATEGORY, symbol=symbol)
    lst = res.get("result", {}).get("list", [])
    if not lst:
        raise RuntimeError(f"instrument info not found for {symbol}: {res}")
    info = lst[0]
    lot = info.get("lotSizeFilter", {})
    qty_step = float(lot.get("qtyStep", "0.001"))
    min_qty = float(lot.get("minOrderQty", "0.001"))
    return qty_step, min_qty

def round_qty(qty: float, step: float):
    """거래소 수량 스텝에 맞게 내림 반올림"""
    return math.floor(qty / step) * step

def get_mark_price(symbol: str) -> float:
    res = session.get_tickers(category=CATEGORY, symbol=symbol)
    lst = res.get("result", {}).get("list", [])
    if not lst:
        raise RuntimeError(f"ticker not found: {res}")
    return float(lst[0]["markPrice"])

def ensure_leverage(symbol: str, lev: int):
    # 양/음 모두 동일 레버리지 설정
    session.set_leverage(
        category=CATEGORY,
        symbol=symbol,
        buyLeverage=str(lev),
        sellLeverage=str(lev),
    )

def get_position_side_and_size(symbol: str):
    """현재 포지션 방향/수량(계약수량)을 반환. 없으면 ('NONE', 0.0)"""
    res = session.get_positions(category=CATEGORY, symbol=symbol)
    lst = res.get("result", {}).get("list", [])
    size_total = 0.0
    side = "NONE"
    # One-way 가정: list 길이가 1일 수 있으나 안전하게 합산.
    for p in lst:
        s = float(p.get("size", "0"))
        if s <= 0:
            continue
        pos_side = p.get("side")  # "Buy" or "Sell"
        size_total += s
        side = pos_side
    return side, size_total

def close_position_if_needed(symbol: str, want_side: str):
    """원하는 방향과 반대인 포지션이 있으면 reduceOnly로 전량 청산"""
    cur_side, cur_size = get_position_side_and_size(symbol)
    if cur_side == "NONE" or cur_size <= 0:
        return None

    # 반대면 청산
    # 현재가 Buy(롱)인데 Sell(숏)으로 가고 싶으면 Buy를 줄이는 게 아니라
    # "Sell reduceOnly" 로 청산
    if cur_side != want_side:
        close_side = "Sell" if cur_side == "Buy" else "Buy"
        r = session.place_order(
            category=CATEGORY,
            symbol=symbol,
            side=close_side,
            orderType="Market",
            qty=str(cur_size),
            reduceOnly=True,
            positionIdx=POSITION_IDX,
            timeInForce="IOC",
        )
        return r
    return None

def open_position(symbol: str, want_side: str, usdt_amount: float):
    """원하는 방향으로 진입. 수량은 USDT/markPrice 기반, 스텝 반올림"""
    price = get_mark_price(symbol)
    step, min_qty = get_symbol_info(symbol)
    raw_qty = usdt_amount / price
    qty = round_qty(raw_qty, step)
    if qty < min_qty:
        qty = min_qty

    r = session.place_order(
        category=CATEGORY,
        symbol=symbol,
        side=want_side,              # "Buy" or "Sell"
        orderType="Market",
        qty=str(qty),
        reduceOnly=False,
        positionIdx=POSITION_IDX,
        timeInForce="IOC",
    )
    return r, qty

# === 라우트 ===
@app.route("/", methods=["GET"])
def health():
    return "OK (Bybit)", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    # 1) JSON 파싱
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        return _resp(400, ok=False, error="Invalid JSON", detail=str(e))

    # 2) 시크릿 검증
    if WEBHOOK_SECRET:
        if not data or data.get("secret") != WEBHOOK_SECRET:
            return _resp(401, ok=False, error="Unauthorized (secret mismatch)")

    signal = str(data.get("signal", "")).upper().strip()
    if signal not in ("BUY", "SELL"):
        return _resp(400, ok=False, error="signal must be BUY or SELL")

    want_side = "Buy" if signal == "BUY" else "Sell"

    # 3
