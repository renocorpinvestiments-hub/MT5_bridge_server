# gcloud_mt5_bootstrap.sh
#!/bin/bash
set -Eeuo pipefail

# ==========================================================
# INSTITUTIONAL GOOGLE CLOUD MT5 + BRIDGE BOOTSTRAP
# One-click setup for MetaTrader 5 + FastAPI Bridge
# ==========================================================

APP_NAME="mt5_bridge"
APP_USER="mt5bot"
APP_DIR="/opt/MT5_BRIDGE"
SERVICE_FILE="/etc/systemd/system/mt5_bridge.service"
ENV_FILE="/etc/mt5_bridge_env"

echo "======================================================"
echo "🚀 Bootstrapping Google Cloud MT5 + Bridge Environment"
echo "======================================================"

# ----------------------------------------------------------
# 1) Create dedicated user
# ----------------------------------------------------------
echo "[1/8] Creating service user..."
if ! id "$APP_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$APP_USER"
fi

mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ----------------------------------------------------------
# 2) Install system dependencies
# ----------------------------------------------------------
echo "[2/8] Installing system dependencies..."

apt-get update
apt-get install -y \
  wget \
  curl \
  unzip \
  software-properties-common \
  python3 \
  python3-venv \
  python3-pip \
  xvfb \
  winbind \
  wine64 \
  cabextract \
  fonts-wine \
  mesa-utils \
  dbus-x11 \
  supervisor

# ----------------------------------------------------------
# 3) Install MetaTrader 5 (Headless + auto-login capable)
# ----------------------------------------------------------
echo "[3/8] Installing MetaTrader 5 via Wine..."

sudo -u "$APP_USER" bash <<EOF
set -e
export WINEPREFIX=/home/$APP_USER/.wine
export DISPLAY=:99

# Start virtual display for headless MT5
Xvfb :99 -screen 0 1024x768x16 &

# Download official MT5 installer
wget -O /home/$APP_USER/mt5setup.exe \
  https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe

# Install MT5 silently
wine64 /home/$APP_USER/mt5setup.exe /silent /auto

echo "MetaTrader 5 installed in Wine environment."
EOF

# ----------------------------------------------------------
# 4) Create secure environment file
# ----------------------------------------------------------
echo "[4/8] Creating secure environment config..."

cat > "$ENV_FILE" <<EOF
# ==== MT5 Credentials ====
MT5_LOGIN=YOUR_MT5_LOGIN
MT5_PASSWORD=YOUR_MT5_PASSWORD
MT5_SERVER=YOUR_MT5_SERVER

# ==== Bridge Security ====
BRIDGE_API_KEY=super-secret-key-change-me

# ==== Trading config ====
SYMBOLS=EURUSD,GBPUSD,XAUUSD,BTCUSD,NAS100,SPX500,ETHUSD,USDJPY,USDCAD,XAGUSD,GBPUSD
MAGIC_NUMBER=987654
EOF

chmod 600 "$ENV_FILE"
chown "$APP_USER:$APP_USER" "$ENV_FILE"

echo "⚠️  IMPORTANT: Edit /etc/mt5_bridge_env with your real credentials!"

# ----------------------------------------------------------
# 5) Deploy your bridge code
# ----------------------------------------------------------
echo "[5/8] Preparing application directory..."

cat > "$APP_DIR/mt5_bridge_server.py" <<'EOF'
# ============================================================
# INSTITUTIONAL MT5 EXECUTION BRIDGE (PRODUCTION SAFE)
# Google Cloud Deployment
# Compatible with Remote Oracle AccountConnector
# ============================================================

import os
import time
import threading
import logging
from collections import deque
from enum import Enum
from typing import Optional, Dict, Set, Any, List

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

SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS",
           "EURUSD,GBPUSD,XAUUSD").split(",")]

TICK_BUFFER_SIZE = 100_000          # increased
UPDATE_BUFFER_SIZE = 50_000         # increased

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

app = FastAPI(title="MT5 Execution Bridge", version="2.0")

tick_buffer = deque(maxlen=TICK_BUFFER_SIZE)
update_buffer = deque(maxlen=UPDATE_BUFFER_SIZE)

tick_lock = threading.Lock()
update_lock = threading.Lock()
mt5_lock = threading.RLock()

tick_seq = 0
update_seq = 0
last_positions: Set[int] = set()

mt5_connected = False
circuit_breaker = False
reject_counter = 0

account_snapshot: Dict[str, Any] = {}

metrics = {
    "last_order_latency_ms": 0.0,
    "avg_order_latency_ms": 0.0,
    "mt5_reconnects": 0,
    "last_tick_time": 0.0,
}

_threads_started = False

# ============================================================
# MT5 CONNECTION
# ============================================================

def init_mt5():
    global mt5_connected

    with mt5_lock:
        if mt5_connected:
            return

        logger.info("Initializing MT5 connection...")

        if MT5_LOGIN:
            connected = mt5.initialize(
                login=MT5_LOGIN,
                password=MT5_PASSWORD,
                server=MT5_SERVER,
            )
        else:
            connected = mt5.initialize()

        mt5_connected = bool(connected)

        if not mt5_connected:
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return

        for s in SYMBOLS:
            mt5.symbol_select(s, True)

        logger.info("MT5 connected and symbols activated")

def shutdown_mt5():
    global mt5_connected
    with mt5_lock:
        if mt5_connected:
            mt5.shutdown()
            mt5_connected = False
            logger.warning("MT5 shutdown")

def watchdog():
    global mt5_connected

    while True:
        try:
            with mt5_lock:
                terminal = mt5.terminal_info()

            if not terminal:
                mt5_connected = False

            if not mt5_connected:
                metrics["mt5_reconnects"] += 1
                shutdown_mt5()
                time.sleep(2)
                init_mt5()

        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        time.sleep(WATCHDOG_INTERVAL)

# ============================================================
# TICK LOOP (SOURCE OF MARKET DATA)
# ============================================================

def tick_loop():
    global tick_seq

    last_ts = {}

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        for symbol in SYMBOLS:
            try:
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

            except Exception as e:
                logger.error(f"Tick error {symbol}: {e}")

        time.sleep(TICK_POLL_INTERVAL)

# ============================================================
# POSITION LOOP (TRADE STATUS)
# ============================================================

def execution_loop():
    global update_seq, last_positions

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        try:
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

        except Exception as e:
            logger.error(f"Execution loop error: {e}")

        time.sleep(POSITION_POLL_INTERVAL)

# ============================================================
# ACCOUNT LOOP (BALANCE / EQUITY)
# ============================================================

def account_loop():
    global account_snapshot

    while True:
        if not mt5_connected:
            time.sleep(1)
            continue

        try:
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
                    "timestamp": time.time(),
                }

        except Exception as e:
            logger.error(f"Account loop error: {e}")

        time.sleep(ACCOUNT_POLL_INTERVAL)

# ============================================================
# AUTH
# ============================================================

def require_auth(x_api_key: Optional[str]):
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(401, detail={"error": BridgeErrorCode.AUTH_FAILED})

# ============================================================
# ORDER MODEL
# ============================================================

class OrderRequest(BaseModel):
    symbol: str
    type: int
    volume: float = Field(gt=0)
    sl: float
    tp: float

# ============================================================
# NEW: TICKS ENDPOINT (FIX FOR YOUR BOT)
# ============================================================

@app.get("/ticks")
def get_ticks(
    after_seq: int = Query(0, ge=0),
    limit: int = Query(MAX_TICKS_PER_RESPONSE, le=MAX_TICKS_PER_RESPONSE),
    x_api_key: str = Header(None),
):
    require_auth(x_api_key)

    with tick_lock:
        items = [t for t in tick_buffer if t["seq"] > after_seq]

    return {
        "ticks": items[:limit],
        "next_seq": tick_seq,
        "count": len(items[:limit]),
    }

# ============================================================
# NEW: UPDATES ENDPOINT (FIX FOR YOUR BOT)
# ============================================================

@app.get("/updates")
def get_updates(
    after_seq: int = Query(0, ge=0),
    limit: int = Query(MAX_UPDATES_PER_RESPONSE, le=MAX_UPDATES_PER_RESPONSE),
    x_api_key: str = Header(None),
):
    require_auth(x_api_key)

    with update_lock:
        items = [u for u in update_buffer if u["seq"] > after_seq]

    return {
        "updates": items[:limit],
        "next_seq": update_seq,
        "count": len(items[:limit]),
    }

# ============================================================
# NEW: ACCOUNT ENDPOINT (FIX FOR YOUR BOT)
# ============================================================

@app.get("/account")
def get_account(x_api_key: str = Header(None)):
    require_auth(x_api_key)

    if not account_snapshot:
        raise HTTPException(503, detail={"error": "ACCOUNT_NOT_READY"})

    return account_snapshot

# ============================================================
# ORDER ENDPOINT (UNCHANGED CORE, SAFER)
# ============================================================

@app.post("/order")
def send_order(req: OrderRequest, x_api_key: str = Header(None)):
    global update_seq, reject_counter, circuit_breaker

    require_auth(x_api_key)

    if not mt5_connected:
        raise HTTPException(503, detail={"error": BridgeErrorCode.CONNECTION_LOST})

    if circuit_breaker:
        raise HTTPException(503, detail={"error": BridgeErrorCode.CIRCUIT_BREAKER})

    if req.symbol not in SYMBOLS:
        raise HTTPException(400, detail={"error": BridgeErrorCode.INVALID_SYMBOL})

    with mt5_lock:
        info = mt5.symbol_info(req.symbol)
        tick = mt5.symbol_info_tick(req.symbol)

    if not info or not tick:
        raise HTTPException(400, detail={"error": BridgeErrorCode.INVALID_SYMBOL})

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

    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
        reject_counter += 1
        if reject_counter >= MAX_REJECTS_BEFORE_HALT:
            circuit_breaker = True

        raise HTTPException(400, detail={
            "error": BridgeErrorCode.BROKER_REJECT,
            "retcode": getattr(result, "retcode", None)
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
# HEARTBEAT
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

# ============================================================
# START THREADS (SAFE)
# ============================================================

def start_background_threads():
    global _threads_started
    if _threads_started:
        return
    _threads_started = True

    init_mt5()

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=execution_loop, daemon=True).start()
    threading.Thread(target=account_loop, daemon=True).start()

start_background_threads()


EOF

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ----------------------------------------------------------
# 6) Create Python virtual environment + install deps
# ----------------------------------------------------------
echo "[6/8] Setting up Python environment..."

sudo -u "$APP_USER" bash <<EOF
set -e
cd $APP_DIR

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install fastapi uvicorn MetaTrader5

EOF

# ----------------------------------------------------------
# 7) Create production systemd service (auto-start)
# ----------------------------------------------------------
echo "[7/8] Creating systemd service..."

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MT5 FastAPI Bridge (Production)
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE

ExecStart=$APP_DIR/.venv/bin/uvicorn mt5_bridge_server:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --log-level info

Restart=always
RestartSec=3
LimitNOFILE=100000

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reexec
systemctl enable mt5_bridge.service

# ----------------------------------------------------------
# 8) Start everything
# ----------------------------------------------------------
echo "[8/8] Starting services..."

systemctl start mt5_bridge.service

echo "======================================================"
echo "✅ SETUP COMPLETE"
echo "Bridge running on: http://<YOUR_GCLOUD_IP>:8000"
echo "Edit credentials in: /etc/mt5_bridge_env"
echo "Check status: systemctl status mt5_bridge"
echo "View logs: journalctl -u mt5_bridge -f"
echo "======================================================"
