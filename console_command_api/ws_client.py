"""
WebSocket 客户端模块。

负责与 WS Server 建立连接、发送命令请求、接收响应。
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from mcdreforged.api.all import *

from .collector import (
    CommandCaptureSession,
    command_execution_lock,
    server_command_collector_manager,
)

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError:
    websockets = None
    WebSocketClientProtocol = Any


logger = logging.getLogger('ConsoleCommandAPI')

# 翻译函数（由 __init__.py 设置）
_translator: Optional[Callable[[str], str]] = None


def set_translator(trans_func: Callable[[str], str]):
    """设置翻译函数"""
    global _translator
    _translator = trans_func


def tr(key: str, *args) -> str:
    """获取翻译文本"""
    if _translator:
        return _translator(key).format(*args) if args else _translator(key)
    return key


class WSClient:
    """WebSocket 客户端，负责与 WS Server 通信"""

    def __init__(self, server_interface: PluginServerInterface, config):
        self.server_interface = server_interface
        self.config = config
        self.ws: Optional[WebSocketClientProtocol] = None
        self.connected = False
        self.running = True
        self._lock = threading.Lock()
        self._receive_thread: Optional[threading.Thread] = None
        self._pending_requests: Dict[str, asyncio.Event] = {}
        self._request_results: Dict[str, Dict[str, Any]] = {}
        self._asyncio_loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """获取或创建 asyncio 事件循环"""
        if self._asyncio_loop is None or self._asyncio_loop.is_closed():
            self._asyncio_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._asyncio_loop)
        return self._asyncio_loop

    def _run_async(self, coro):
        """在线程中运行异步代码"""
        loop = self._get_loop()
        return loop.run_until_complete(coro)

    async def _async_connect(self):
        """异步连接到 WS Server"""
        if websockets is None:
            self.server_interface.logger.error(tr('error_websockets_not_installed'))
            raise RuntimeError('websockets library not installed')

        # 检查 token
        if not self.config.token:
            self.server_interface.logger.error(tr('error_no_token'))
            self.running = False
            return

        reconnect_delay = self.config.reconnect_interval
        while self.running:
            try:
                self.ws = await websockets.connect(
                    self.config.ws_url,
                    extra_headers={'Authorization': f'Bearer {self.config.token}'}
                )
                with self._lock:
                    self.connected = True
                self.server_interface.logger.info(tr('connected'))

                # 发送注册消息
                await self.ws.send(json.dumps({
                    'type': 'register',
                    'server_name': self.config.server_name,
                    'token': self.config.token,
                }))
                self.server_interface.logger.info(f'Registered as server: {self.config.server_name}')

                await self._async_receive()
            except websockets.exceptions.InvalidStatusCode as e:
                with self._lock:
                    self.connected = False
                    self.ws = None
                self.server_interface.logger.warning(tr('connection_failed', str(e)))
                if self.running:
                    self.server_interface.logger.info(tr('reconnecting'))
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
            except Exception as e:
                with self._lock:
                    self.connected = False
                    self.ws = None
                if 'Invalid token' in str(e):
                    self.server_interface.logger.warning(f'Token invalid, waiting {reconnect_delay}s to reconnect...')
                    if self.running:
                        await asyncio.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 60)
                    continue
                self.server_interface.logger.warning(tr('connection_failed', str(e)))
                reconnect_delay = self.config.reconnect_interval
                if self.running:
                    self.server_interface.logger.info(tr('reconnecting'))
                    await asyncio.sleep(reconnect_delay)

    async def _async_receive(self):
        """异步接收消息"""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    self.server_interface.logger.error(f'Invalid JSON from WS Server: {message}')
                except Exception as e:
                    self.server_interface.logger.error(f'Error handling WS message: {e}')
        except Exception as e:
            if self.running:
                self.server_interface.logger.error(f'WS receive error: {e}')
            raise  # 让异常传播，触发重连延迟

    async def _handle_message(self, data: Dict[str, Any]):
        """处理收到的消息"""
        msg_type = data.get('type')

        if msg_type == 'command':
            # 收到命令请求，转发给 MCDR
            request_id = data.get('request_id')
            command = data.get('command')
            if request_id and command:
                result = await self._execute_command_async(command, request_id)
                if self.ws and self.connected:
                    await self.ws.send(json.dumps({
                        'type': 'response',
                        'request_id': request_id,
                        'server_name': self.config.server_name,
                        **result,
                    }))
        elif msg_type == 'ping':
            # 心跳响应
            pass
        elif msg_type == 'error':
            # 服务端错误
            self.server_interface.logger.error(f'WS Server error: {data.get("message")}')

    def _execute_mcdr_command(self, command: str, request_id: str) -> Dict[str, Any]:
        """执行 MCDR 命令"""
        with CommandCaptureSession(self.server_interface) as session:
            try:
                self.server_interface.execute_command(command, source=session.source)
                timed_out = session.collector.wait_for_complete(
                    total_timeout=self.config.timeout,
                    idle_timeout=self.config.idle_timeout,
                )
                output = session.collector.get_output()
                return {
                    'code': 200,
                    'msg': 'success',
                    'data': {
                        'request_id': request_id,
                        'command': command,
                        'command_type': 'mcdr',
                        'output': output,
                        'output_text': '\n'.join(output),
                        'timed_out': timed_out,
                    }
                }
            except Exception as e:
                output = session.collector.get_output()
                return {
                    'code': 500,
                    'msg': str(e),
                    'data': {
                        'request_id': request_id,
                        'command': command,
                        'command_type': 'mcdr',
                        'output': output,
                        'output_text': '\n'.join(output),
                    }
                }

    def _execute_server_command(self, command: str, request_id: str) -> Dict[str, Any]:
        """执行 MC 服务端命令"""
        if not self.server_interface.is_server_running():
            return {
                'code': 503,
                'msg': 'Minecraft server is not running',
                'data': None
            }

        collector = server_command_collector_manager.create_collector(request_id)
        try:
            self.server_interface.execute(f'say [CMD_API] START {request_id}')
            time.sleep(0.1)
            self.server_interface.execute(command)
            time.sleep(0.1)
            self.server_interface.execute(f'say [CMD_API] END {request_id}')

            timed_out = not collector.event.wait(timeout=self.config.timeout)
            return {
                'code': 200,
                'msg': 'success',
                'data': {
                    'request_id': request_id,
                    'command': command,
                    'command_type': 'minecraft',
                    'output': collector.buffer.copy(),
                    'output_text': '\n'.join(collector.buffer),
                    'timed_out': timed_out,
                }
            }
        except Exception as e:
            return {
                'code': 500,
                'msg': str(e),
                'data': {
                    'request_id': request_id,
                    'command': command,
                    'command_type': 'minecraft',
                    'output': collector.buffer.copy(),
                    'output_text': '\n'.join(collector.buffer),
                }
            }
        finally:
            server_command_collector_manager.remove_collector(request_id)

    async def _execute_command_async(self, command: str, request_id: str) -> Dict[str, Any]:
        """异步执行命令（在线程池中运行阻塞代码）"""
        loop = asyncio.get_event_loop()
        command = command.strip()
        if not command:
            return {
                'code': 400,
                'msg': 'Command cannot be empty',
                'data': None
            }

        # 判断命令类型并执行
        if command.startswith('!!'):
            return await loop.run_in_executor(
                None,
                lambda: self._execute_mcdr_command(command, request_id)
            )
        else:
            return await loop.run_in_executor(
                None,
                lambda: self._execute_server_command(command, request_id)
            )

    def start(self):
        """启动 WebSocket 客户端"""
        if websockets is None:
            self.server_interface.logger.error('websockets library not installed! Please run: pip install websockets')
            return

        self._receive_thread = threading.Thread(
            target=lambda: self._run_async(self._async_connect()),
            name='ConsoleCommandAPI-WS-Client',
            daemon=True,
        )
        self._receive_thread.start()
        self.server_interface.logger.info(f'WS Client starting, connecting to: {self.config.ws_url}')

    def stop(self):
        """停止 WebSocket 客户端"""
        self.running = False
        with self._lock:
            if self.ws:
                try:
                    # 通过关闭循环来中断 websockets.recv
                    if self._asyncio_loop and not self._asyncio_loop.is_closed():
                        self._asyncio_loop.call_soon_threadsafe(
                            lambda: self._asyncio_loop.stop()
                        )
                except Exception:
                    pass
                self.ws = None
                self.connected = False
        self.server_interface.logger.info(tr('client_stopped'))


class HealthTracker:
    """跟踪各服务器的健康状态"""

    def __init__(self):
        self._servers: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def update(self, server_name: str, connected: bool):
        with self._lock:
            self._servers[server_name] = connected

    def get_status(self) -> Dict[str, bool]:
        with self._lock:
            return self._servers.copy()


ws_client: Optional[WSClient] = None
health_tracker = HealthTracker()


def set_server_context(server_interface, config):
    """注入 MCDR 的服务接口和插件配置"""
    global _server_interface, _config
    _server_interface = server_interface
    _config = config


def start_client(config):
    """启动 WebSocket 客户端（供 __init__.py 调用）"""
    global ws_client
    ws_client = WSClient(_server_interface, config)
    ws_client.start()


def stop_client():
    """停止 WebSocket 客户端（供 __init__.py 调用）"""
    global ws_client
    if ws_client:
        ws_client.stop()
        ws_client = None


_config = None
_server_interface = None
