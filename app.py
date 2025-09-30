import os, math, json
from flask import Flask, request, jsonify, abort
from pybit.unified_trading import HTTP
from datetime import datetime

# --------- 환경변수 ----------
API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"   # 기본 testnet
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")             # 예: BTCUSDT
LEVERAGE = int(os.getenv("LEVERAGE", "5"))          # 예: 5
POSITION_USDT = float(os.getenv("POSITION_USDT", "50"))  # 진입금액(USDT)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")    # TradingView alert JSON의 secret과 동일

CATEGORY = "linear"  # Bybit USDT-Perp

# --------- Bybit 세션 ----------
session = HTTP(
    testnet=USE_TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET,
    timeout=10
)

app = Flask(__name__)

# --------- 유틸 ----------
def _get_instrument_info(symbol):
    info = session.get_instruments_info(category=CATEGORY, symbol=symbol)
    return info["result"]["list"][0]

def _get_tick_price(symbol):
    t = session.get_tickers(category=CATEGORY, symbol=SYMBOL)
    return float(t["result"]["list"][0]["lastPrice"])

def _qty_round(qty, step):
    return math.floor(qty / step) * step

def _usdt_to_qty(symbol, usdt):
    """USDT 금액을 거래 가능 수량(qty)으로 변환(스텝/최소수량 반영)"""
    price = _get_tick_price(symbol)
    info = _get_instrument_info(symbol)
    lot_step = float(info["lotSizeFilter"]["qtyStep"])
    min_qty = float(info["lotSizeFilter"]["minOrderQty"])
    raw = usdt / price
    qty = max(_qty_round(raw, lot_step), min_qty)
    # 소수점 자릿수 맞추기
    step_dec = abs(int(round(math.log10(lot_step), 0)))
    return float(f"{qty:.{max(0, step_dec)}f}")

def _get_position_amt(symbol):
    """현재 포지션 수량(+롱, -숏, 0 무포지션)"""
    r = session.get_positions(category=CATEGORY, symbol=symbol)
    lst = r["result"]["list"]
    # Bybit는 롱/숏이 별도 항목일 수 있음(헤지모드). 합산해서 표시.
    amt = 0.0
    for pos in lst:
        sz = float(pos["size"] or 0)
        side = pos["side"]  # "Buy" or "Sell"
        amt += sz if side == "Buy" else -sz
    return amt

def _ensure_settings():
    """원웨이 모드(헤지 끔) + 레버리지 설정 시도 (가능한 경우에만)"""
    try:
        # 헤지모드 끄기(=원웨이)
        session.switch_position_mode(category=CATEGORY, symbol=SYMBOL, mode="MergedSingle")  # 원웨이
    except Exception:
        pass
    try:
        # 레버리지 설정 (롱/숏 동시 설정)
        session.set_leverage(category=CATEGORY, symbol=SYMBOL,
                             buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE))
    except Exception:
        pass

_ensure_settings()

def _close_opposite_if_needed(next_side):
    """반대 포지션이 있으면 reduceOnly 시장가로 청산"""
    amt = _get_position_amt(SYMBOL)
    if amt == 0:
        return
    if next_side == "BUY" and amt < 0:
        session.place_order(category=CATEGORY, symbol=SYMBOL,
                            side="Buy", orderType="Market", qty=str(abs(amt)),
                            reduceOnly=True)
    elif next_side == "SELL" and amt > 0:
        session.place_order(category=CATEGORY, symbol=SYMBOL,
                            side="Sell", orderType="Market", qty=str(abs(amt)),
                            reduceOnly=True)

def _open_position(side):
    qty = _usdt_to_qty(SYMBOL, POSITION_USDT)
    session.place_order(category=CATEGORY, symbol=SYMBOL,
                        side=("Buy" if side == "BUY" else "Sell"),
                        orderType="Market", qty=str(qty))

# --------- 엔드포인트 ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
    except Exception:
        abort(400, "Invalid JSON")

    # 보안: secret 일치 여부 확인(설정한 경우에만)
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        abort(401, "Bad secret")

    signal = (data.get("signal") or "").upper()
    symbol = data.get("symbol", SYMBOL)
    tf = data.get("timeframe", "")

    if symbol != SYMBOL:
        abort(400, f"Symbol mismatch: {symbol}")

    if signal not in ("BUY", "SELL"):
        abort(400, "Signal must be BUY or SELL")

    # 반대 포지션 청산
    _close_opposite_if_needed(signal)

    # 이미 동일 방향 보유 중이면 스킵(중복 진입 방지)
    amt = _get_position_amt(SYMBOL)
    if signal == "BUY" and amt > 0:
        return jsonify({"status": "ok", "note": "already long"}), 200
    if signal == "SELL" and amt < 0:
        return jsonify({"status": "ok", "note": "already short"}), 200

    # 새 진입
    _open_position(signal)

    return jsonify({
        "status": "ok",
        "executed": signal,
        "symbol": symbol,
        "tf": tf,
        "server_time": datetime.utcnow().isoformat() + "Z"
    }), 200

@app.route("/", methods=["GET"])
def health():
    return "OK (Bybit)", 200
