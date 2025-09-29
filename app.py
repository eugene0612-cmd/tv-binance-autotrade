import os, math, json
from flask import Flask, request, jsonify, abort
from binance.um_futures import UMFutures
from binance.error import ClientError

# --------- 환경변수 읽기 ----------
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"   # 기본 testnet
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
POSITION_USDT = float(os.getenv("POSITION_USDT", "50"))  # 50 USDT어치 진입
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # TradingView 메시지에 넣을 비밀키(선택)

BASE_URL = "https://testnet.binancefuture.com" if USE_TESTNET else None
client = UMFutures(key=API_KEY, secret=API_SECRET, base_url=BASE_URL)

app = Flask(__name__)

# --------- 유틸: 거래 규격(스텝사이즈) 맞춰 반올림 ----------
def _get_qty_step(symbol):
    info = client.exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    min_qty = float(f["minQty"])
                    return step, min_qty
    return 0.001, 0.001  # fallback

def _round_step(qty, step):
    return math.floor(qty/step)*step

def _usdt_to_qty(symbol, usdt):
    price = float(client.ticker_price(symbol=symbol)["price"])
    step, min_qty = _get_qty_step(symbol)
    raw = usdt / price
    qty = max(_round_step(raw, step), min_qty)
    return round(qty, 8)

# --------- 초기 세팅: 원웨이모드/레버리지 ----------
def init_account():
    try:
        # 원웨이 모드(이중포지션 방지)
        client.change_position_mode(dualSidePosition="false")
    except ClientError as e:
        # 이미 설정된 경우 에러가 날 수 있음 → 무시
        pass
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
    except ClientError:
        pass

init_account()

def get_position_amt(symbol):
    pos_list = client.position_information(symbol=symbol)
    # USDT-M 선물은 리스트 1개 리턴
    amt = float(pos_list[0]["positionAmt"])
    return amt

def close_opposite_if_needed(next_side):
    amt = get_position_amt(SYMBOL)
    if amt == 0:
        return
    # amt>0 = 롱 보유, amt<0 = 숏 보유
    if next_side == "BUY" and amt < 0:
        client.new_order(symbol=SYMBOL, side="BUY", type="MARKET",
                         quantity=abs(amt), reduceOnly="true")
    elif next_side == "SELL" and amt > 0:
        client.new_order(symbol=SYMBOL, side="SELL", type="MARKET",
                         quantity=abs(amt), reduceOnly="true")

def open_position(side):
    qty = _usdt_to_qty(SYMBOL, POSITION_USDT)
    client.new_order(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
    except Exception:
        abort(400, "Invalid JSON")

    # (선택) 보안: 메시지에 secret이 있으면 검사
    if WEBHOOK_SECRET:
        if data.get("secret") != WEBHOOK_SECRET:
            abort(401, "Bad secret")

    signal = (data.get("signal") or "").upper()
    symbol = data.get("symbol", SYMBOL)
    tf = data.get("timeframe", "")

    if symbol != SYMBOL:
        # 심볼 고정 운영 (필요시 허용)
        abort(400, f"Symbol mismatch: {symbol}")

    if signal not in ("BUY", "SELL"):
        abort(400, "Signal must be BUY or SELL")

    # 1) 반대 포지션 있으면 청산(reduceOnly)
    close_opposite_if_needed(signal)

    # 2) 동일 방향 포지션 이미 있으면? → 재진입 방지(옵션)
    amt = get_position_amt(SYMBOL)
    if signal == "BUY" and amt > 0:
        return jsonify({"status":"ok", "note":"already long"}), 200
    if signal == "SELL" and amt < 0:
        return jsonify({"status":"ok", "note":"already short"}), 200

    # 3) 새 진입
    open_position(signal)

    return jsonify({"status": "ok", "executed": signal, "symbol": symbol, "tf": tf}), 200

@app.route("/", methods=["GET"])
def health():
    return "OK", 200
