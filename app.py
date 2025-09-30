import os
import math
from flask import Flask, request, jsonify, abort
from pybit.unified_trading import HTTP

API_KEY       = os.getenv("BYBIT_API_KEY", "")
API_SECRET    = os.getenv("BYBIT_API_SECRET", "")
WEBHOOK_SECRET= os.getenv("WEBHOOK_SECRET", "")
USE_TESTNET   = os.getenv("USE_TESTNET", "true").lower() == "true"
SYMBOL        = os.getenv("SYMBOL", "BTCUSDT")
LEVERAGE      = int(os.getenv("LEVERAGE", "5"))
POSITION_USDT = float(os.getenv("POSITION_USDT", "50"))

app = Flask(__name__)
session = HTTP(testnet=USE_TESTNET, api_key=API_KEY, api_secret=API_SECRET)

def _ok(resp):
    return isinstance(resp, dict) and resp.get("retCode") == 0

def _ensure_ok(resp, where=""):
    if not _ok(resp):
        raise ValueError(f"{where} retCode={resp.get('retCode')} retMsg={resp.get('retMsg')} resp={resp}")

def get_mark_price(symbol: str) -> float:
    r = session.get_tickers(category="linear", symbol=symbol)
    print("[DBG] get_tickers resp:", r)
    _ensure_ok(r, "get_tickers")
    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        raise ValueError("get_tickers empty list")
    return float(lst[0]["lastPrice"])

def get_lot_filters(symbol: str):
    r = session.get_instruments_info(category="linear", symbol=symbol)
    print("[DBG] get_instruments_info resp:", r)
    _ensure_ok(r, "get_instruments_info")
    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        raise ValueError("get_instruments_info empty list")
    f = lst[0]["lotSizeFilter"]
    min_qty = float(f["minOrderQty"])
    qty_step = float(f["qtyStep"])
    return min_qty, qty_step

def round_qty(qty: float, step: float, min_qty: float) -> float:
    if qty < min_qty:
        return 0.0
    return math.floor(qty / step) * step

def set_oneway_and_leverage(symbol: str, leverage: int):
    try:
        session.switch_position_mode(category="linear", symbol=symbol, mode=0)
    except Exception as e:
        print("[WARN] switch_position_mode:", e)
    try:
        session.set_leverage(category="linear", symbol=symbol,
                             buyLeverage=str(leverage), sellLeverage=str(leverage))
    except Exception as e:
        print("[WARN] set_leverage:", e)

def get_position_amt(symbol: str) -> float:
    r = session.get_positions(category="linear", symbol=symbol)
    print("[DBG] get_positions resp:", r)
    if not _ok(r):
        return 0.0
    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        return 0.0
    p = lst[0]
    side = p.get("side")
    size = float(p.get("size") or 0.0)
    return size if side == "Buy" else -size if side == "Sell" else 0.0

def market_close(side: str, qty: float):
    if qty <= 0:
        return
    try:
        resp = session.place_order(
            category="linear", symbol=SYMBOL, side=side,
            orderType="Market", qty=qty, reduceOnly=True, timeInForce="GoodTillCancel"
        )
        print(f"[CLOSE] {side} {qty} ->", resp)
        _ensure_ok(resp, "place_order close")
    except Exception as e:
        print("[ERR] market_close:", e)
        raise

def market_open(side: str, qty: float):
    try:
        resp = session.place_order(
            category="linear", symbol=SYMBOL, side=side,
            orderType="Market", qty=qty, reduceOnly=False, timeInForce="GoodTillCancel"
        )
        print(f"[OPEN] {side} {qty} ->", resp)
        _ensure_ok(resp, "place_order open")
    except Exception as e:
        print("[ERR] market_open:", e)
        raise

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        print("[DBG] incoming:", data)

        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            abort(401, "Bad secret")

        signal = (data.get("signal") or "").upper()
        if signal not in ("BUY", "SELL"):
            abort(400, "Signal must be BUY or SELL")

        # 환경변수 누락 확인
        if not API_KEY or not API_SECRET:
            abort(500, "Missing BYBIT_API_KEY/SECRET")

        # 심볼 유효성 체크 (트뷰가 symbol 보내더라도 우리는 환경변수 기준으로 운용)
        symbol = SYMBOL

        # 레버리지/모드 설정
        set_oneway_and_leverage(symbol, LEVERAGE)

        # 수량 계산
        price = get_mark_price(symbol)
        min_qty, step = get_lot_filters(symbol)
        raw_qty = POSITION_USDT / price
        qty = round_qty(raw_qty, step, min_qty)
        if qty <= 0:
            abort(400, f"Qty too small. POSITION_USDT={POSITION_USDT}, price={price}, min_qty={min_qty}")

        # 반대 포지션 청산
        cur = get_position_amt(symbol)
        if signal == "BUY" and cur < 0:
            market_close("Buy", abs(cur))
        if signal == "SELL" and cur > 0:
            market_close("Sell", cur)

        # 같은 방향 보유 시 중복 진입 방지
        cur = get_position_amt(symbol)
        if signal == "BUY" and cur > 0:
            return jsonify({"status": "ok", "note": "already long"}), 200
        if signal == "SELL" and cur < 0:
            return jsonify({"status": "ok", "note": "already short"}), 200

        # 신규 진입
        side = "Buy" if signal == "BUY" else "Sell"
        market_open(side, qty)

        return jsonify({"status": "ok", "executed": signal, "qty": qty, "price": price}), 200

    except Exception as e:
        # 어떤 에러인지 로그에 남기고 500으로 리턴
        print("[FATAL] webhook error:", repr(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return "OK (Bybit)", 200
