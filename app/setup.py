#!/usr/bin/env python3
import os
import ssl

from setuptools import setup

# 忽略 SSL 验证问题（某些网络环境下需要）
if not os.environ.get("PYTHONHTTPSVERIFY", "") and getattr(ssl, "_create_unverified_context", None):
    ssl._create_default_https_context = ssl._create_unverified_context

setup(
    name="electro",
    version="1.0.0",
    description="BlueOS Serial Reader Extension",
    license="MIT",
    install_requires=[
        # fastapi 和 pydantic 版本必须配套
        "fastapi==0.111.0",
        "pydantic==2.7.1",
        "starlette==0.37.2",
        # web 服务器（纯 Python 版，不依赖 C 扩展）
        "uvicorn==0.29.0",
        "aiofiles==23.2.1",
        # 串口
        "pyserial==3.5",
        # 日志
        "loguru==0.7.2",
        # 已移除 uvloop 和 httptools：
        # ARM 平台上编译后被安装为 UNKNOWN，setup.py 找不到报错。
        # uvicorn 不依赖它们也可以正常运行。
    ],
)
