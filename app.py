import os
import math
from flask import Flask, request, jsonify, abort
from pybit.unified_trading import HTTP

# -------------------------
# 환경변수 (Render에서 넣음)
# -------------------------
API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")          # Bybit Linear USDT Perp (예: BTCUSDT, ETHUSDT)
LEVERAGE = int(os.getenv("LEVERAGE", "5"))       # 양방향 동일 레버리지 적용
POSITION_USDT = float(os.getenv("POSITION_USDT", "50"))  # 1회 진입에 사용할 USDT
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "") # 트뷰 메시지의 secret과 일치해야 함
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"  # 메인넷: false

# 메인넷 사용 (USE_TESTNET=False)
client = HTTP(
    testnet=USE_TESTNET,       # 메인넷이면 False, 테스트넷이면 True
    api_key=API_KEY,
    api_secret=API_SECRET
)

app = Flask(__name__)

# --------- 유틸 ---------
def get_mark_price(symbol: str) -> float:
    """현재 마크가격 (선물)"""
    res = client.get_tickers(category="linear", symbol=symbol)
    price = float(res["result"]["list"][0]["lastPrice"])
    return price

def set_leverage(symbol: str, lev: int):
    """양방향 레버리지 설정 (Bybit는 buy/sell 따로)"""
    client.set_leverage(category="linear", symbol=symbol,
                        buyLeverage=str(lev), sellLeverage=str(lev))

def get_position_side_qty(symbol: str):
    """
    현재 보유 포지션 조회.
    return:
      ("Long" or "Short" or None, qty_float)
    """
    res = client.get_positions(category="linear", symbol=symbol)
    items = res["result"]["list"]
    side = None
    qty = 0.0
    # Bybit는 리스트로 포지션을 반환(롱/숏 각각 있을 수 있음). One-way면 한쪽만 존재.
    for it in items:
        sz = float(it.get("size", "0") or 0)
        if sz > 0:
            side = it.get("side")  # "Buy" or "Sell"
            qty = sz
            break
    if side is None:
        return None, 0.0
    return ("Long" if side == "Buy" else "Short"), qty

def round_qty(symbol: str, qty: float) -> float:
    """
    BTCUSDT는 보통 0.001 스텝. 간단히 3자리로 반올림.
    엄밀히 하려면 exchangeInfo로 lot size 확인 필요.
    """
    return max(0.0, round(qty, 3))

def close_position(symbol: str, side_to_close: str, qty: float):
    """reduce-only 시장가 청산"""
    if qty <= 0:
        return
    if side_to_close == "Long":
        # 롱 청산 → Sell reduce-only
        client.place_order(category="linear", symbol=symbol,
                           side="Sell", orderType="Market",
                           reduceOnly=True, qty=str(qty))
    elif side_to_close == "Short":
        # 숏 청산 → Buy reduce-only
        client.place_order(category="linear", symbol=symbol,
                           side="Buy", orderType="Market",
                           reduceOnly=True, qty=str(qty))

def open_position(symbol: str, signal: str):
    """
    새 포지션 진입 (시장가).
    수량은 (POSITION_USDT * LEVERAGE / mark_price) 로 산출.
    """
    price = get_mark_price(symbol)
    raw_qty = (POSITION_USDT * LEVERAGE) / price
    qty = round_qty(symbol, raw_qty)
    if qty <= 0:
        raise ValueError("계산된 수량이 0입니다. POSITION_USDT 또는 LEVERAGE 값을 확인하세요.")
    if signal == "BUY":
        client.place_order(category="linear", symbol=symbol,
                           side="Buy", orderType="Market",
                           qty=str(qty))
    else:
        client.place_order(category="linear", symbol=symbol,
                           side="Sell", orderType="Market",
                           qty=str(qty))

def close_opposite_if_needed(symbol: str, signal: str):
    """
    반대포지션 있으면 전량 청산.
    동일방향 이미 보유면 '재진입하지 않음'.
    """
    cur_side, cur_qty = get_position_side_qty(symbol)
    if cur_side is None or cur_qty == 0:
        return "empty", 0.0

    if signal == "BUY":
        if cur_side == "Short":
            close_position(symbol, "Short", cur_qty)
            return "closed_short", cur_qty
        else:
            return "already_long", cur_qty
    else:  # SELL
        if cur_side == "Long":
            close_position(symbol, "Long", cur_qty)
            return "closed_long", cur_qty
        else:
            return "already_short", cur_qty

# ---------- 라우트 ----------
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    if not request.is_json:
        return jsonify({"status": "error", "reason": "invalid json"}), 400
    data = request.get_json(silent=True) or {}

    # 1) Secret 검사
    if WEBHOOK_SECRET:
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"status": "error", "reason": "bad secret"}), 403

    # 2) 파라미터
    signal = str(data.get("signal", "")).upper()   # BUY/SELL
    symbol = str(data.get("symbol", SYMBOL)).upper()
    timeframe = str(data.get("timeframe", ""))

    if signal not in ("BUY", "SELL"):
        return jsonify({"status": "error", "reason": "signal must be BUY or SELL"}), 400

    # 3) 메인넷인지 한번 더 확정(로그용)
    if USE_TESTNET:
        return jsonify({"status": "error", "reason": "server is running on TESTNET"}), 400

    # 4) 레버리지 설정 (최초 1회 또는 매회 안전하게)
    set_leverage(symbol, LEVERAGE)

    # 5) 반대포지션 청산 / 동일방향이면 스킵
    action, qty = close_opposite_if_needed(symbol, signal)
    if action.startswith("already_"):
        return jsonify({"status": "ok", "note": action, "symbol": symbol}), 200

    # 6) 새 진입
    open_position(symbol, signal)
    return jsonify({"status": "ok", "executed": signal, "symbol": symbol}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
