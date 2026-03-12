"""
BlueOS 通用 HTTP 工具函数
"""
import urllib.request

from loguru import logger


def request(url: str):
    """
    发送 GET 请求，成功返回响应文本，失败返回 None
    """
    try:
        return urllib.request.urlopen(url, timeout=1).read().decode()
    except Exception as error:
        logger.warning(f"GET 请求失败 {url}: {error}")
        return None


def post(url: str, data: str):
    """
    发送 POST 请求（JSON），成功返回响应内容，失败返回 None

    修复：之前 jsondataasbytes 编码后没有被实际使用
    """
    try:
        # ✅ 用编码后的字节，而不是原始字符串
        jsondataasbytes = data.encode("utf-8")

        req = urllib.request.Request(url, jsondataasbytes)  # ✅ 传编码后的数据
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req) as response:
            return response.read()

    except Exception as error:
        logger.warning(f"POST 请求失败 {url}: {error}")
        logger.warning(f"请求数据：{data}")
        return None
