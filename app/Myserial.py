"""
串口状态管理、数据模型、串口读取后台任务
"""

import asyncio
import json
import time
from collections import deque
from datetime import datetime
from typing import Optional

import serial
import serial.tools.list_ports
from fastapi import WebSocket
from loguru import logger
from pydantic import BaseModel

# ── 常量 ──────────────────────────────────────────────────────────────────────

MAX_HISTORY  = 500
DEFAULT_PORT = "/dev/ttyAMA0"
DEFAULT_BAUD = 115200


# ── 全局状态 ──────────────────────────────────────────────────────────────────

class SerialState:
    def __init__(self):
        self.port:        str            = DEFAULT_PORT
        self.baud:        int            = DEFAULT_BAUD
        self.connected:   bool           = False
        self.error:       Optional[str]  = None
        self.serial:      Optional[serial.Serial] = None
        self.history:     deque          = deque(maxlen=MAX_HISTORY)
        self.total_bytes: int            = 0
        self.total_lines: int            = 0
        self.start_time:  float          = time.time()
        self.clients:     list[WebSocket] = []

# 整个程序共享这一个实例
state = SerialState()


# ── 数据模型 ──────────────────────────────────────────────────────────────────

class SerialConfig(BaseModel):
    """浏览器POST /config 时发来的请求体"""
    port: str
    baud: int


class SerialLine:
    """把一行原始串口字节封装成带时间戳的对象"""
    def __init__(self, raw: str):
        self.timestamp = datetime.now().isoformat(timespec="milliseconds")
        self.raw   = raw.strip()
        self.index = 0  # 由调用方赋值

    def to_dict(self) -> dict:
        return {
            "type":      "line",
            "timestamp": self.timestamp,
            "raw":       self.raw,
            "index":     self.index,
        }


# ── WebSocket 广播 ────────────────────────────────────────────────────────────

async def broadcast(message: dict):
    """把 message 以 JSON 文本推送给所有已连接的浏览器"""
    dead    = []
    payload = json.dumps(message)

    for ws in state.clients:
        try:
            await ws.send_text(payload)
        except Exception:
            # 发送失败说明这个连接已经断了，标记待删除
            dead.append(ws)

    for ws in dead:
        state.clients.remove(ws)


# ── 串口后台读取任务 ──────────────────────────────────────────────────────────

async def serial_reader():
    """
    后台长驻任务：
    1. 尝试打开串口
    2. 循环读行，封装成 SerialLine，存历史并广播
    3. 出错后等 3 秒重试，实现自动重连
    """
    while True:

        # ── 第一步：打开串口 ──────────────────────────────────────────────────
        try:
            state.serial = serial.Serial(
                port=state.port,
                baudrate=state.baud,
                timeout=1.0,       # readline 最多等 1 秒
            )
            state.connected = True
            state.error     = None
            logger.info(f"串口已打开：{state.port} @ {state.baud}")
            await broadcast({
                "type": "status", "connected": True,
                "port": state.port, "baud": state.baud
            })

        except Exception as e:
            state.connected = False
            state.error     = str(e)
            logger.warning(f"串口打开失败：{e}")
            await broadcast({"type": "status", "connected": False, "error": str(e)})
            await asyncio.sleep(3)
            continue   # 回到 while True，重新尝试打开

        # ── 第二步：持续读取 ──────────────────────────────────────────────────
        try:
            loop = asyncio.get_event_loop()

            while True:
                # serial.readline() 是阻塞调用，放进线程池执行，避免卡住事件循环
                raw_bytes: bytes = await loop.run_in_executor(
                    None, state.serial.readline
                )

                if not raw_bytes:
                    continue   # timeout=1.0 到了但没数据，继续等

                state.total_bytes += len(raw_bytes)

                # 把字节解码成字符串，遇到非UTF-8字符用?替换
                try:
                    text = raw_bytes.decode("utf-8", errors="replace")
                except Exception:
                    text = repr(raw_bytes)

                # 过滤纯空白行
                if text.strip():
                    state.total_lines += 1
                    line        = SerialLine(text)
                    line.index  = state.total_lines

                    line_dict = line.to_dict()
                    state.history.append(line_dict)  # 存入历史
                    await broadcast(line_dict)        # 推送给浏览器

        except Exception as e:
            state.connected = False
            state.error     = str(e)
            logger.error(f"串口读取出错：{e}")
            await broadcast({"type": "status", "connected": False, "error": str(e)})

            if state.serial:
                try:
                    state.serial.close()
                except Exception:
                    pass

            await asyncio.sleep(3)   # 等 3 秒后重新打开串口
