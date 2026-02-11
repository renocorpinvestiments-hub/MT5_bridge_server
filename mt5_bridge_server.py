import os
import time
import threading
import logging
from collections import deque
from enum import Enum
from typing import Optional, Set

import MetaTrader5 as mt5
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

# ============================================================
# CONFIG (PRODUCTION SAFE)
# ============================================================

MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

API_KEY = os.getenv("BRIDGE_API_KEY", "CHANGE_ME")
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", "123456"))

SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]

TICK_BUFFER_SIZE = 50000
UPDATE_BUFFER_SIZE = 10000

TICK_POLL_INTERVAL = 0.01  # 10ms
POSITION_POLL_INTERVAL = 0.5
MT5_WATCHDOG_INTERVAL = 5

MAX_TICKS_PER_RESPONSE = 5000
MAX_UPDATES_PER_RESPONSE = 1000

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mt5_bridge")

# ============================================================
# ERROR ENUM
# ============================================================

class BridgeErrorCode(str, Enum):
    AUTH_FAILED = "AUTH_FAILED"
    CONNECTION_LOST = "CONNECTION_LOST"
    BROKER_REJECT = "BROKER_REJECT"
    INTERNAL_ERROR = "INTERNAL_ERROR"

# ============================================================
# STATE
# ============================================================

app = FastAPI()

tick_buffer = deque(maxlen=TICK_BUFFER_SIZE)
update_buffer = deque(maxlen=UPDATE_BUFFER_SIZE)

tick_lock = threading.Lock()
update_lock = threading.Lock()

tick_seq = 0
last_positions = set()
mt5_connected = False

# ============================================================
# MT5 INIT + WATCHDOG
# ============================================================

def init_mt5():
    global mt5_connected

    if MT5_LOGIN:
        connected = mt5.initialize(
            login=MT5_LOGIN,
            password=MT5_PASSWORD,
            server=MT5_SERVER
        )
    else:
        connected = mt5.initialize()

    mt5_connected = bool(connected)

    if mt5_connected:
        logger.info("MT5 connected")
    else:
        logger.error("MT5 initialization failed")

def mt5_watchdog():
    global mt5_connected

    while True:
        if not mt5_connected:
            logger.warning("Attempting MT5 reconnect...")
            init_mt5()
        time.sleep(MT5_WATCHDOG_INTERVAL)

init_mt5()
threading.Thread(target=mt5_watchdog, daemon=True).start()

# ============================================================
# TICK POLLER (LOSSLESS + ORDERED)
# ============================================================

def tick_poller():
    global tick_seq

    last_ts = {}

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        for symbol in SYMBOLS:
            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                continue

            ts = tick.time
            if last_ts.get(symbol) == ts:
                continue

            last_ts[symbol] = ts
            tick_seq += 1

            tick_data = {
                "seq": tick_seq,
                "symbol": symbol,
                "ts": float(ts),
                "bid": tick.bid,
                "ask": tick.ask,
                "volume": tick.volume_real or 0.0,
            }

            with tick_lock:
                tick_buffer.append(tick_data)

        time.sleep(TICK_POLL_INTERVAL)

# ============================================================
# POSITION TRACKER
# ============================================================

def execution_tracker():
    global last_positions

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        positions = mt5.positions_get()
        current = set()

        if positions:
            for p in positions:
                current.add(p.ticket)

        closed = last_positions - current

        if closed:
            with update_lock:
                for ticket in closed:
                    update_buffer.append({
                        "event": "POSITION_CLOSED",
                        "ticket": ticket,
                        "ts": time.time()
                    })

        last_positions = current
        time.sleep(POSITION_POLL_INTERVAL)

threading.Thread(target=tick_poller, daemon=True).start()
threading.Thread(target=execution_tracker, daemon=True).start()

# ============================================================
# AUTH
# ============================================================

def require_auth(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail={"error": BridgeErrorCode.AUTH_FAILED}
        )

# ============================================================
# REST ENDPOINTS
# ============================================================

@app.get("/heartbeat")
def heartbeat():
    return {
        "status": "alive",
        "mt5_connected": mt5_connected,
        "last_seq": tick_seq,
        "tick_buffer_size": len(tick_buffer),
        "update_buffer_size": len(update_buffer),
        "timestamp": time.time()
    }

@app.get("/account")
def account(x_api_key: str = Header(None)):
    require_auth(x_api_key)

    if not mt5_connected:
        raise HTTPException(status_code=503,
                            detail={"error": BridgeErrorCode.CONNECTION_LOST})

    info = mt5.account_info()
    if not info:
        raise HTTPException(status_code=500,
                            detail={"error": BridgeErrorCode.INTERNAL_ERROR})

    return {
        "balance": info.balance,
        "equity": info.equity,
        "free_margin": info.margin_free,
        "leverage": info.leverage,
        "currency": info.currency,
        "is_demo": info.trade_mode == 0,
    }

@app.get("/ticks")
def get_ticks(
    after_seq: int = Query(0),
    symbols: Optional[str] = Query(None),
    x_api_key: str = Header(None)
):
    require_auth(x_api_key)

    if not mt5_connected:
        raise HTTPException(status_code=503)

    symbol_filter: Optional[Set[str]] = None
    if symbols:
        symbol_filter = set(symbols.split(","))

    with tick_lock:
        filtered = [
            t for t in tick_buffer
            if t["seq"] > after_seq and
               (symbol_filter is None or t["symbol"] in symbol_filter)
        ]

        return {
            "last_seq": tick_seq,
            "count": len(filtered),
            "ticks": filtered[:MAX_TICKS_PER_RESPONSE]
        }

@app.get("/updates")
def get_updates(x_api_key: str = Header(None)):
    require_auth(x_api_key)

    with update_lock:
        batch = list(update_buffer)[:MAX_UPDATES_PER_RESPONSE]

    return {
        "count": len(batch),
        "updates": batch
    }

# ============================================================
# ORDER EXECUTION
# ============================================================

class OrderRequest(BaseModel):
    symbol: str
    type: int
    volume: float
    price: float
    sl: float
    tp: float

@app.post("/order")
def send_order(req: OrderRequest, x_api_key: str = Header(None)):
    require_auth(x_api_key)

    if not mt5_connected:
        raise HTTPException(status_code=503,
                            detail={"error": BridgeErrorCode.CONNECTION_LOST})

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": req.symbol,
        "volume": req.volume,
        "type": req.type,
        "price": req.price,
        "sl": req.sl,
        "tp": req.tp,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": "OracleBot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    result = mt5.order_send(request)

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("Order rejected: %s", result.retcode)
        raise HTTPException(
            status_code=400,
            detail={
                "error": BridgeErrorCode.BROKER_REJECT,
                "retcode": result.retcode
            }
        )

    with update_lock:
        update_buffer.append({
            "event": "ORDER_FILLED",
            "ticket": result.order,
            "ts": time.time()
        })

    return {
        "status": "ok",
        "ticket": result.order
    }
