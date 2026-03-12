"""
FastAPI 主程序
- REST 接口：/status  /history  /ports  /config  /history(DELETE)
- WebSocket：/ws  实时推送串口数据
- 静态文件：/ 托管前端页面
"""

import asyncio
import json
import time

import serial.tools.list_ports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from loguru import logger

# 从 Myserial 导入需要的东西
from Myserial import (
    SerialConfig,
    SerialState,
    broadcast,
    serial_reader,
    state,
)

# ── 应用初始化 ────────────────────────────────────────────────────────────────

SERVICE_NAME = "Serial Reader"

app = FastAPI(
    title="Serial Reader API",
    description="BlueOS 扩展：读取串口数据并通过 WebSocket 推送到浏览器",
    version="1.0.0",
)

logger.info(f"Starting {SERVICE_NAME}!")


# ── 启动事件：开启串口后台任务 ────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    logger.info(f"{SERVICE_NAME} is starting up...")
    # 创建后台任务，程序运行期间一直在后台读串口
    asyncio.create_task(serial_reader())
    logger.info(f"{SERVICE_NAME} has started.")


# ── REST 接口 ─────────────────────────────────────────────────────────────────

@app.get("/status", summary="获取当前串口状态和统计信息")
async def get_status():
    uptime = time.time() - state.start_time
    return {
        "connected":     state.connected,
        "port":          state.port,
        "baud":          state.baud,
        "error":         state.error,
        "total_lines":   state.total_lines,
        "total_bytes":   state.total_bytes,
        "uptime_seconds": round(uptime, 1),
        "history_count": len(state.history),
    }


@app.get("/history", summary="获取历史记录（最近500行）")
async def get_history():
    return {"lines": list(state.history)}


@app.get("/ports", summary="列出系统中所有可用串口")
async def list_ports():
    ports = [
        {"device": p.device, "description": p.description}
        for p in serial.tools.list_ports.comports()
    ]
    return {"ports": ports}


@app.post("/config", summary="修改串口和波特率，立即生效")
async def set_config(config: SerialConfig):
    state.port = config.port
    state.baud = config.baud
    state.connected = False
    # 关闭当前连接，serial_reader 会自动重新打开
    if state.serial and state.serial.is_open:
        state.serial.close()
    logger.info(f"Config updated → {config.port} @ {config.baud}")
    return {"ok": True, "port": state.port, "baud": state.baud}


@app.delete("/history", summary="清空历史记录，并通知所有浏览器")
async def clear_history():
    state.history.clear()
    await broadcast({"type": "clear"})   # ✅ 直接调用模块函数
    return {"ok": True}


# ── WebSocket 接口 ────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    浏览器连接后：
    1. 立刻发送当前状态 + 历史记录（让页面有初始内容）
    2. 加入 state.clients，之后串口每来一行都会推过来
    3. 断开时从列表移除
    """
    await websocket.accept()
    state.clients.append(websocket)
    logger.info(f"浏览器连接，当前共 {len(state.clients)} 个客户端")

    # 新连接立刻把现有数据发过去
    await websocket.send_text(json.dumps({
        "type": "init",
        "status": {
            "connected": state.connected,
            "port":      state.port,
            "baud":      state.baud,
            "error":     state.error,
        },
        "history": list(state.history),
    }))

    try:
        while True:
            # 等浏览器发消息（比如 ping 心跳），保持连接活跃
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        state.clients.remove(websocket)
        logger.info(f"浏览器断开，当前共 {len(state.clients)} 个客户端")


# ── 静态文件（前端页面） ──────────────────────────────────────────────────────

# 放在最后，避免路由被 "/" 拦截
app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")
