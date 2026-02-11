# ============================================================
# INSTITUTIONAL MT5 EXECUTION BRIDGE (FINAL)
# ============================================================

import os
import time
import threading
import logging
from collections import deque
from enum import Enum
from typing import Optional, Dict, Set

import MetaTrader5 as mt5
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

# ============================================================
# CONFIG
# ============================================================

MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

API_KEY = os.getenv("BRIDGE_API_KEY", "CHANGE_ME")
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", "123456"))

SYMBOLS = os.getenv("SYMBOLS", "EURUSD,GBPUSD,XAUUSD").split(",")

TICK_BUFFER_SIZE = 50000
UPDATE_BUFFER_SIZE = 20000

TICK_POLL_INTERVAL = 0.01
POSITION_POLL_INTERVAL = 0.25
ACCOUNT_POLL_INTERVAL = 1.0
WATCHDOG_INTERVAL = 5

MAX_TICKS_PER_RESPONSE = 5000
MAX_UPDATES_PER_RESPONSE = 2000

MAX_SPREAD_POINTS = 50
MAX_ORDER_LATENCY_MS = 800
MAX_REJECTS_BEFORE_HALT = 5

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
    DESYNC_DETECTED = "DESYNC_DETECTED"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INVALID_SYMBOL = "INVALID_SYMBOL"
    MARGIN_INSUFFICIENT = "MARGIN_INSUFFICIENT"

# ============================================================
# APP + STATE
# ============================================================

app = FastAPI()

tick_buffer = deque(maxlen=TICK_BUFFER_SIZE)
update_buffer = deque(maxlen=UPDATE_BUFFER_SIZE)

tick_lock = threading.Lock()
update_lock = threading.Lock()
mt5_lock = threading.Lock()

tick_seq = 0
update_seq = 0
last_positions: Set[int] = set()

mt5_connected = False
circuit_breaker = False
reject_counter = 0

account_snapshot: Dict[str, any] = {}

metrics = {
    "last_order_latency_ms": 0.0,
    "avg_order_latency_ms": 0.0,
    "mt5_reconnects": 0,
    "last_tick_time": 0.0,
}

# ============================================================
# MT5 CONNECTION
# ============================================================

def init_mt5():
    global mt5_connected

    with mt5_lock:
        if MT5_LOGIN:
            connected = mt5.initialize(
                login=MT5_LOGIN,
                password=MT5_PASSWORD,
                server=MT5_SERVER,
            )
        else:
            connected = mt5.initialize()

    mt5_connected = bool(connected)

    if mt5_connected:
        logger.info("MT5 connected")
        for s in SYMBOLS:
            mt5.symbol_select(s, True)
    else:
        logger.error("MT5 connection failed")

def watchdog():
    global mt5_connected

    while True:
        with mt5_lock:
            if not mt5.terminal_info():
                mt5_connected = False
                metrics["mt5_reconnects"] += 1
                init_mt5()
        time.sleep(WATCHDOG_INTERVAL)

# ============================================================
# TICK ENGINE (LOSSLESS)
# ============================================================

def tick_loop():
    global tick_seq

    last_ts = {}

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        for symbol in SYMBOLS:
            with mt5_lock:
                tick = mt5.symbol_info_tick(symbol)

            if not tick:
                continue

            if last_ts.get(symbol) == tick.time:
                continue

            last_ts[symbol] = tick.time

            with tick_lock:
                tick_seq += 1
                tick_buffer.append({
                    "seq": tick_seq,
                    "symbol": symbol,
                    "ts": float(tick.time),
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "volume": tick.volume_real or 0.0,
                })

            metrics["last_tick_time"] = time.time()

        time.sleep(TICK_POLL_INTERVAL)

# ============================================================
# POSITION TRACKER
# ============================================================

def execution_loop():
    global update_seq, last_positions

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        with mt5_lock:
            positions = mt5.positions_get()

        current = set(p.ticket for p in positions) if positions else set()
        closed = last_positions - current

        if closed:
            with update_lock:
                for ticket in closed:
                    update_seq += 1
                    update_buffer.append({
                        "seq": update_seq,
                        "event": "POSITION_CLOSED",
                        "ticket": ticket,
                        "ts": time.time()
                    })

        last_positions = current
        time.sleep(POSITION_POLL_INTERVAL)

# ============================================================
# ACCOUNT LOOP
# ============================================================

def account_loop():
    global account_snapshot

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        with mt5_lock:
            info = mt5.account_info()

        if info:
            account_snapshot = {
                "balance": info.balance,
                "equity": info.equity,
                "free_margin": info.margin_free,
                "leverage": info.leverage,
                "currency": info.currency,
                "is_demo": info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
            }

        time.sleep(ACCOUNT_POLL_INTERVAL)

# ============================================================
# AUTH
# ============================================================

def require_auth(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(401, detail={"error": BridgeErrorCode.AUTH_FAILED})

# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/heartbeat")
def heartbeat():
    return {
        "status": "alive",
        "mt5_connected": mt5_connected,
        "circuit_breaker": circuit_breaker,
        "tick_seq": tick_seq,
        "update_seq": update_seq,
        "metrics": metrics,
        "timestamp": time.time(),
    }

@app.get("/account")
def get_account(x_api_key: str = Header(None)):
    require_auth(x_api_key)
    return account_snapshot

@app.get("/ticks")
def get_ticks(after_seq: int = Query(0), x_api_key: str = Header(None)):
    require_auth(x_api_key)

    with tick_lock:
        oldest = tick_seq - len(tick_buffer)

        if after_seq < oldest:
            raise HTTPException(409, detail={"error": BridgeErrorCode.DESYNC_DETECTED})

        data = [t for t in tick_buffer if t["seq"] > after_seq]

        return {
            "last_seq": tick_seq,
            "ticks": data[:MAX_TICKS_PER_RESPONSE],
        }

@app.get("/updates")
def get_updates(after_seq: int = Query(0), x_api_key: str = Header(None)):
    require_auth(x_api_key)

    with update_lock:
        oldest = update_seq - len(update_buffer)

        if after_seq < oldest:
            raise HTTPException(409, detail={"error": BridgeErrorCode.DESYNC_DETECTED})

        data = [u for u in update_buffer if u["seq"] > after_seq]

        return {
            "last_seq": update_seq,
            "updates": data[:MAX_UPDATES_PER_RESPONSE],
        }

# ============================================================
# ORDER ENGINE
# ============================================================

class OrderRequest(BaseModel):
    symbol: str
    type: int
    volume: float = Field(gt=0)
    sl: float
    tp: float

@app.post("/order")
def send_order(req: OrderRequest, x_api_key: str = Header(None)):
    global update_seq, reject_counter, circuit_breaker

    require_auth(x_api_key)

    if not mt5_connected:
        raise HTTPException(503, detail={"error": BridgeErrorCode.CONNECTION_LOST})

    if circuit_breaker:
        raise HTTPException(503, detail={"error": BridgeErrorCode.CIRCUIT_BREAKER})

    with mt5_lock:
        info = mt5.symbol_info(req.symbol)
        tick = mt5.symbol_info_tick(req.symbol)

    spread = abs(tick.ask - tick.bid) / info.point
    if spread > MAX_SPREAD_POINTS:
        raise HTTPException(400, detail={"error": BridgeErrorCode.SPREAD_TOO_WIDE})

    price = tick.ask if req.type == mt5.ORDER_TYPE_BUY else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": req.symbol,
        "volume": req.volume,
        "type": req.type,
        "price": price,
        "sl": req.sl,
        "tp": req.tp,
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    start = time.time()
    with mt5_lock:
        result = mt5.order_send(request)
    latency = (time.time() - start) * 1000

    metrics["last_order_latency_ms"] = latency
    metrics["avg_order_latency_ms"] = (
        metrics["avg_order_latency_ms"] * 0.9 + latency * 0.1
    )

    if latency > MAX_ORDER_LATENCY_MS:
        circuit_breaker = True

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        reject_counter += 1
        if reject_counter >= MAX_REJECTS_BEFORE_HALT:
            circuit_breaker = True

        raise HTTPException(400, detail={
            "error": BridgeErrorCode.BROKER_REJECT,
            "retcode": result.retcode
        })

    reject_counter = 0

    with update_lock:
        update_seq += 1
        update_buffer.append({
            "seq": update_seq,
            "event": "ORDER_FILLED",
            "ticket": result.order,
            "latency_ms": latency,
            "ts": time.time()
        })

    return {"status": "ok", "ticket": result.order, "latency_ms": latency}

# ============================================================
# START THREADS
# ============================================================

init_mt5()
threading.Thread(target=watchdog, daemon=True).start()
threading.Thread(target=tick_loop, daemon=True).start()
threading.Thread(target=execution_loop, daemon=True).start()
threading.Thread(target=account_loop, daemon=True).start()
