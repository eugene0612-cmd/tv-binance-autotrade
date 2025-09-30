import os
import math
import logging
from flask import Flask, request, jsonify

# pybit v5 (unified trading)
from pybit.unified_trading import HTTP

# -----------------------------------------------------------------------------
# 기본 설정
# -----------------------------------------------------------------------------
app = Flask(__name__)
log = app.logger
log.setLevel(logging.INFO)

# 환경 변수
BYBIT_API_KEY     = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET  = os.getenv("BYBIT_API_SECRET", "").strip()
USE_TESTNET       = os.getenv("USE_TESTNET", "true").strip().lower() in ("1", "true", "yes", "y")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "").strip()           # 예: banya419!
DEFAULT_SYMBOL    = os.getenv("SYMBOL", "BTCUSDT").upper()
DEFAULT_LEVERAGE  = int(os.getenv("LEVERAGE", "5"))
POSITION_USDT     = float(os.getenv("POSITION_USDT", "50"))           # 진입 노치널 (USDT)
# 카테고리: 바이비트 선물(USDT 무기한)은 대부분 "linear"
CATEGORY          = "linear"

# -----------------------------------------------------------------------------
# Bybit 클라이언트
# -----------------------------------------------------------------------------
def create_client():
    """
    pybit v5 unified trading 클라이언트 생성 (테스트넷/메인넷)
    """
    client = HTTP(
        testnet=USE_TESTNET,
        api_key=BYBIT_API_KEY or None,
        api_secret=BYBIT_API_SECRET or None,
    )
    return client

client = create_client()

# -----------------------------------------------------------------------------
# 유틸 함수
# -----------------------------------------------------------------------------
def get_mark_price(symbol: str) -> float:
    """
    현재가(틱커) 조회 (linear/USDT 무기한)
    """
    resp = client.get_tickers(category=CATEGORY, symbol=symbol)
    # 응답 예시: {"retCode":0,"result":{"list":[{"lastPrice":"114000","..."}]}}
    if resp.get("retCode") != 0:
        raise RuntimeError(f"get_tickers failed: {resp}")
    lst = resp.get("result", {}).get("list", [])
    if not lst:
        raise RuntimeError(f"ticker list empty: {resp}")
    last_str = lst[0].get("lastPrice") or lst[0].get("markPrice")
    if not last_str:
        raise RuntimeError(f"no last/mark price in: {lst[0]}")
    return float(last_str)

def ensure_leverage(symbol: str, lev: int):
    """
    심볼 레버리지 설정(롱/숏 동일 레버리지)
    """
    try:
        client.set_leverage(
            category=CATEGORY,
            symbol=symbol,
            buyLeverage=str(lev),
            sellLeverage=str(lev),
        )
    except Exception as e:
        # 레버리지 설정 실패해도 거래는 진행 가능하므로 경고로만 남김
        log.warning(f"set_leverage warn: {e}")

def get_position_qty_side(symbol: str):
    """
    포지션 조회 → (side, qty) 반환
    side: "Buy" / "Sell" / None  (없으면 qty=0)
    qty : 현재 보유 수량(코인 단위, float)
    """
    resp = client.get_positions(category=CATEGORY, symbol=symbol)
    if resp.get("retCode") != 0:
        raise RuntimeError(f"get_positions failed: {resp}")
    data = resp.get("result", {}).get("list", [])
    # unified v5: 포지션은 최대 2개 (Buy/Sell)로 나뉘어 제공될 수 있음
    total_buy = 0.0
    total_sell = 0.0
    for p in data:
        side = p.get("side")                  # "Buy" or "Sell"
        sz   = float(p.get("size", "0") or 0) # 계약 수량(코인 단위)
        if side == "Buy":
            total_buy += sz
        elif side == "Sell":
            total_sell += sz
    if total_buy > 0 and total_sell > 0:
        # 허용하지 않을 구성(양방향 포지션) → 예외적으로 큰 쪽 기준으로 반환
        if total_buy >= total_sell:
            return "Buy", total_buy - total_sell
        else:
            return "Sell", total_sell - total_buy
    elif total_buy > 0:
        return "Buy", total_buy
    elif total_sell > 0:
        return "Sell", total_sell
    else:
        return None, 0.0

def close_opposite_if_needed(symbol: str, signal: str):
    """
    반대 포지션이 있으면 reduceOnly 시장가로 청산
    """
    cur_side, cur_qty = get_position_qty_side(symbol)
    if cur_qty <= 0:
        return

    # BUY 신호면 숏(Sell) 포지션 정리, SELL 신호면 롱(Buy) 정리
    if signal == "BUY" and cur_side == "Sell":
        # 숏 정리 → Buy reduceOnly
        client.place_order(
            category=CATEGORY,
            symbol=symbol,
            side="Buy",
            orderType="Market",
            qty=str(cur_qty),
            reduceOnly=True,
            timeInForce="IOC",
        )
        log.info(f"Closed SHORT {cur_qty} {symbol} (reduceOnly)")
    elif signal == "SELL" and cur_side == "Buy":
        # 롱 정리 → Sell reduceOnly
        client.place_order(
            category=CATEGORY,
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=str(cur_qty),
            reduceOnly=True,
            timeInForce="IOC",
        )
        log.info(f"Closed LONG {cur_qty} {symbol} (reduceOnly)")

def open_position(symbol: str, signal: str, usdt_notional: float):
    """
    신규 진입 (시장가)
    POSITION_USDT(USDT) / 현재가 → 코인 수량(Qty) 계산
    """
    price = get_mark_price(symbol)
    qty = usdt_notional / price
    # 거래소 최소 수량 고려(소수 점절단). 안전하게 1e-6 단위 반올림
    qty = float(f"{qty:.6f}")
    if qty <= 0:
        raise RuntimeError(f"Calculated qty <= 0 (price={price}, notional={usdt_notional})")

    side = "Buy" if signal == "BUY" else "Sell"
    resp = client.place_order(
        category=CATEGORY,
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(qty),
        reduceOnly=False,
        timeInForce="IOC",
    )
    if resp.get("retCode") != 0:
        raise RuntimeError(f"place_order failed: {resp}")

    log.info(f"Opened {side} {qty} {symbol} @~{price} (≈{usdt_notional} USDT)")

# -----------------------------------------------------------------------------
# 라우터
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    TradingView Webhook 엔드포인트
    - body JSON 예시:
      {
        "secret": "banya419!",
        "signal": "BUY",            // or "SELL"
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "price": "{{close}}",
        "time": "{{timenow}}",
        "alert_name": "{{alert_name}}"
      }
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"status": "error", "message": "invalid JSON"}), 400

        # 시크릿 검증
        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"status": "error", "message": "secret mismatch"}), 401

        # 신호/심볼
        signal     = str(data.get("signal", "")).upper()
        symbol     = str(data.get("symbol", DEFAULT_SYMBOL)).upper()
        timeframe  = str(data.get("timeframe", "")).lower()
        alert_name = data.get("alert_name")

        if signal not in ("BUY", "SELL"):
            # 잘못된 신호는 무시(200 반환해 트뷰 알림 재시도 방지)
            return jsonify({"status": "ignored", "reason": f"unknown signal {signal}"}), 200

        # 레버리지 보장 (실패해도 거래 진행)
        ensure_leverage(symbol, DEFAULT_LEVERAGE)

        # 1) 반대 포지션 정리
        close_opposite_if_needed(symbol, signal)

        # 2) 신규 진입
        open_position(symbol, signal, POSITION_USDT)

        return jsonify({
            "status": "ok",
            "executed": signal,
            "symbol": symbol,
            "tf": timeframe,
            "note": "order placed",
        }), 200

    except Exception as e:
        log.exception("webhook error")
        return jsonify({"status": "error", "message": str(e)}), 500

# -----------------------------------------------------------------------------
# 로컬 실행용 (Render에서는 gunicorn이 실행)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    # 개발 로컬 테스트 시 True로. Render는 gunicorn 사용.
    app.run(host="0.0.0.0", port=port, debug=False)
