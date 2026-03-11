"""
BlueOS Serial Monitor Extension
Reads serial data from UART and broadcasts via WebSocket
"""

import asyncio
import json
import time
from collections import deque
from datetime import datetime
from typing import Optional

import serial
import serial.tools.list_ports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

MAX_HISTORY = 500       # lines kept in memory
DEFAULT_PORT = "/dev/ttyAMA0"
DEFAULT_BAUD = 115200

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Serial Monitor",
    description="BlueOS extension: read UART and stream to browser",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── State ─────────────────────────────────────────────────────────────────────

class SerialState:
    def __init__(self):
        self.port: str = DEFAULT_PORT
        self.baud: int = DEFAULT_BAUD
        self.connected: bool = False
        self.error: Optional[str] = None
        self.serial: Optional[serial.Serial] = None
        self.history: deque = deque(maxlen=MAX_HISTORY)
        self.total_bytes: int = 0
        self.total_lines: int = 0
        self.start_time: float = time.time()
        self.clients: list[WebSocket] = []

state = SerialState()

# ── Data models ───────────────────────────────────────────────────────────────

class SerialConfig(BaseModel):
    port: str
    baud: int

class SerialLine:
    def __init__(self, raw: str):
        self.timestamp = datetime.now().isoformat(timespec="milliseconds")
        self.raw = raw.strip()
        self.index = 0  # set by caller

    def to_dict(self):
        return {
            "type": "line",
            "timestamp": self.timestamp,
            "raw": self.raw,
            "index": self.index,
        }

# ── Serial reader task ────────────────────────────────────────────────────────

async def broadcast(message: dict):
    """Send JSON message to all connected WebSocket clients."""
    dead = []
    payload = json.dumps(message)
    for ws in state.clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.clients.remove(ws)

async def serial_reader():
    """Background task: open serial port and read lines."""
    while True:
        # Try to open port
        try:
            state.serial = serial.Serial(
                port=state.port,
                baudrate=state.baud,
                timeout=1.0,
            )
            state.connected = True
            state.error = None
            logger.info(f"Opened {state.port} @ {state.baud}")
            await broadcast({"type": "status", "connected": True, "port": state.port, "baud": state.baud})
        except Exception as e:
            state.connected = False
            state.error = str(e)
            logger.warning(f"Cannot open serial port: {e}")
            await broadcast({"type": "status", "connected": False, "error": str(e)})
            await asyncio.sleep(3)
            continue

        # Read loop
        try:
            loop = asyncio.get_event_loop()
            while True:
                # Run blocking readline in executor to not block event loop
                raw_bytes = await loop.run_in_executor(
                    None, state.serial.readline
                )
                if not raw_bytes:
                    continue

                state.total_bytes += len(raw_bytes)

                try:
                    text = raw_bytes.decode("utf-8", errors="replace")
                except Exception:
                    text = repr(raw_bytes)

                if text.strip():
                    state.total_lines += 1
                    line = SerialLine(text)
                    line.index = state.total_lines
                    state.history.append(line.to_dict())
                    await broadcast(line.to_dict())

        except Exception as e:
            state.connected = False
            state.error = str(e)
            logger.error(f"Serial read error: {e}")
            await broadcast({"type": "status", "connected": False, "error": str(e)})
            if state.serial:
                try:
                    state.serial.close()
                except Exception:
                    pass
            await asyncio.sleep(3)

# ── REST endpoints ────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    asyncio.create_task(serial_reader())

@app.get("/status")
async def get_status():
    uptime = time.time() - state.start_time
    return {
        "connected": state.connected,
        "port": state.port,
        "baud": state.baud,
        "error": state.error,
        "total_lines": state.total_lines,
        "total_bytes": state.total_bytes,
        "uptime_seconds": round(uptime, 1),
        "history_count": len(state.history),
    }

@app.get("/history")
async def get_history():
    return {"lines": list(state.history)}

@app.get("/ports")
async def list_ports():
    ports = [
        {"device": p.device, "description": p.description}
        for p in serial.tools.list_ports.comports()
    ]
    return {"ports": ports}

@app.post("/config")
async def set_config(config: SerialConfig):
    """Change port/baud. Closes current connection; reader will reconnect."""
    state.port = config.port
    state.baud = config.baud
    state.connected = False
    if state.serial and state.serial.is_open:
        state.serial.close()
    logger.info(f"Config updated → {config.port} @ {config.baud}")
    return {"ok": True, "port": state.port, "baud": state.baud}

@app.delete("/history")
async def clear_history():
    state.history.clear()
    await broadcast({"type": "clear"})
    return {"ok": True}

# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.clients.append(websocket)
    logger.info(f"WS client connected. Total: {len(state.clients)}")

    # Send current status + history immediately on connect
    await websocket.send_text(json.dumps({
        "type": "init",
        "status": {
            "connected": state.connected,
            "port": state.port,
            "baud": state.baud,
            "error": state.error,
        },
        "history": list(state.history),
    }))

    try:
        while True:
            # Keep connection alive; client can send ping
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        state.clients.remove(websocket)
        logger.info(f"WS client disconnected. Total: {len(state.clients)}")

# ── Static frontend ───────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="static")
