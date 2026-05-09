# Console Command API

[English](./README.md) | [简体中文](./README.zh_cn.md)

Console Command API 是一个 MCDReforged 插件，用于通过 HTTP API 执行命令并获取输出结果。

它同时支持两类命令：

- 以 `!!` 开头的命令会被当作 MCDR 命令执行
- 不以 `!!` 开头的命令会被当作 Minecraft 服务端控制台命令执行

## 功能

- 通过 HTTP 执行 MCDR 命令
- 通过 HTTP 执行 Minecraft 服务端控制台命令
- 在 HTTP 响应中返回捕获到的命令输出
- 使用 Bearer Token 进行鉴权
- 提供简单的健康检查接口

## 依赖

- 与当前 MCDR 运行环境兼容的 Python
- `mcdreforged>=2.0.0`
- `fastapi`
- `uvicorn`
- `pydantic`

## 配置

插件首次加载时会自动生成配置文件。

当前配置项如下：

```json
{
  "token": "",
  "timeout": 5.0,
  "idle_timeout": 0.2,
  "host": "0.0.0.0",
  "port": 8000
}
```

### 字段说明

- `token`：HTTP API 使用的 Bearer Token。如果为空，插件首次加载时会自动生成。
- `timeout`：等待命令输出的最长时间，单位为秒。
- `idle_timeout`：MCDR 命令输出收集时额外等待的静默窗口，单位为秒。
- `host`：HTTP 服务绑定地址。
- `port`：HTTP 服务监听端口。

## API

### 鉴权

所有接口都使用 Bearer Token：

```http
Authorization: Bearer <token>
```

### `GET /health`

返回插件当前健康状态。

示例响应：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "status": "ok",
    "server_running": true
  }
}
```

### `POST /execute`

执行命令并返回捕获到的输出。

请求体：

```json
{
  "command": "!!MCDR plugin list"
}
```

### 命令路由规则

- `!!MCDR plugin list` -> 按 MCDR 命令执行
- `list` -> 按 Minecraft 服务端控制台命令执行

成功响应示例：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "request_id": "2b0f0f34-2f0f-4d97-9e2f-123456789abc",
    "command": "!!MCDR plugin list",
    "command_type": "mcdr",
    "output": [
      "..."
    ],
    "output_text": "...",
    "timed_out": false
  }
}
```

错误响应示例：

```json
{
  "code": 401,
  "msg": "Invalid authentication token",
  "data": null
}
```

## 说明

- MCDR 命令通过自定义 `CommandSource` 和临时日志捕获来收集输出。
- Minecraft 服务端命令通过服务端输出中的开始/结束标记来截取响应。
- Minecraft 服务端命令要求游戏服务端已经处于运行状态。
- 为了避免并发请求之间的输出互相串台，插件会串行执行命令请求。

## 许可证

本项目使用 MIT License。详见 [LICENSE](./LICENSE)。
