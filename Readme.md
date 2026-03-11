# BlueOS Serial Monitor Extension

实时读取树莓派串口数据并在浏览器中显示的 BlueOS 扩展应用。

## 功能特性

- 🔌 **实时串口读取** — 支持所有标准 UART 端口（ttyAMA0, ttyUSB0 等）
- 📡 **WebSocket 推送** — 数据实时推送到浏览器，零轮询延迟
- 🔍 **正则过滤** — 支持关键词/正则表达式实时过滤日志
- 💾 **历史记录** — 内存保留最近 500 行，新连接自动补全历史
- ⬇️ **导出日志** — 一键导出带时间戳的完整日志文件
- 🔄 **断线重连** — 串口或 WebSocket 断开后自动重连
- ⚙️ **动态配置** — 运行时切换端口和波特率，无需重启容器

## 项目结构

```
blueos-serial-extension/
├── backend/
│   ├── main.py              # FastAPI 后端
│   └── requirements.txt     # Python 依赖
├── frontend/
│   └── index.html           # 单页面前端（工业终端风格）
├── config/
│   └── register_service     # BlueOS 服务注册描述
├── Dockerfile
├── docker-compose.yml       # 本地开发用
└── README.md
```

## 快速开始

### 方式一：本地开发（docker-compose）

```bash
# 克隆/下载本项目
cd blueos-serial-extension

# 构建并启动
docker-compose up --build

# 访问 http://localhost:5000
```

### 方式二：部署到 BlueOS

#### 1. 构建并推送镜像

```bash
# 替换为你的 Docker Hub 用户名
docker buildx build --platform linux/arm64,linux/amd64 \
  -t yourname/blueos-serial-monitor:latest \
  --push .
```

#### 2. 在 BlueOS 中安装扩展

1. 打开 BlueOS Web 界面
2. 进入 **Extensions Manager**
3. 点击 **Add Extension**
4. 输入镜像名称：`yourname/blueos-serial-monitor:latest`
5. 安装完成后，在扩展列表中找到 **Serial Monitor** 并打开

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/status` | 获取连接状态和统计信息 |
| GET | `/history` | 获取历史日志（最近500行） |
| GET | `/ports` | 列出系统中所有可用串口 |
| POST | `/config` | 修改端口和波特率 |
| DELETE | `/history` | 清空历史记录 |
| WS | `/ws` | WebSocket 实时数据流 |
| GET | `/docs` | Swagger API 文档 |

### POST /config 示例

```json
{
  "port": "/dev/ttyUSB0",
  "baud": 9600
}
```

## WebSocket 消息格式

### 连接时收到（init）
```json
{
  "type": "init",
  "status": { "connected": true, "port": "/dev/ttyAMA0", "baud": 115200 },
  "history": [...]
}
```

### 新数据行（line）
```json
{
  "type": "line",
  "timestamp": "2024-01-15T10:23:45.123",
  "raw": "sensor_value=3.14",
  "index": 42
}
```

### 连接状态变化（status）
```json
{
  "type": "status",
  "connected": false,
  "error": "Port not found"
}
```

## 树莓派串口说明

| 端口 | 说明 |
|------|------|
| `/dev/ttyAMA0` | 硬件 UART（GPIO 14/15）|
| `/dev/serial0` | 主串口别名 |
| `/dev/ttyUSB0` | USB 转串口适配器 |

### 启用树莓派硬件 UART

```bash
# 在 /boot/config.txt 中添加：
enable_uart=1

# 禁用串口控制台（避免冲突）：
sudo systemctl disable serial-getty@ttyAMA0.service
```

## 环境变量

可通过 docker-compose 或 Docker 命令传入：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SERIAL_PORT` | `/dev/ttyAMA0` | 默认串口（计划支持） |
| `SERIAL_BAUD` | `115200` | 默认波特率（计划支持） |

## 常见问题

**Q: 串口无法打开，提示 Permission denied**
```bash
# 确保容器以 privileged 模式运行（docker-compose.yml 中已配置）
# 或手动添加设备权限：
sudo chmod 666 /dev/ttyAMA0
```

**Q: 在 BlueOS 中看不到扩展**
- 检查 Docker 镜像是否为 arm64 架构（树莓派）
- 查看 BlueOS 日志：Extensions → Logs

**Q: 数据乱码**
- 确认波特率设置正确
- 检查发送端是否为 UTF-8 编码，否则使用十六进制查看模式（计划支持）
