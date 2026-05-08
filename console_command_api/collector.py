"""
命令输出收集工具。

支持两类命令：
1. MCDR 命令：通过自定义 CommandSource 和日志捕获收集输出
2. MC 服务端命令：通过 START/END 标记在服务端输出中截取响应
"""
import logging
import re
import threading
import time
from typing import Dict, List

from mcdreforged.command.command_source import CommandSource
from mcdreforged.minecraft.rtext.text import RTextBase
from mcdreforged.permission.permission_level import PermissionLevel

# 为了避免多个 HTTP 请求的输出互相串台，命令执行统一串行化。
command_execution_lock = threading.Lock()

# MC 服务端命令输出使用标记包围，便于从 on_info 中截取。
MARKER_PATTERN = re.compile(r'\[CMD_API\] (START|END) ([a-f0-9\-]+)')


class CommandOutputCollector:
	"""缓存一次命令执行期间收集到的文本输出。"""

	def __init__(self):
		self._buffer: List[str] = []
		self._condition = threading.Condition()
		self._last_update_time = time.monotonic()

	def add_text(self, text) -> None:
		"""将任意 MCDR 文本对象转换为纯文本并写入缓冲区。"""
		text_str = RTextBase.from_any(text).to_plain_text()
		lines = text_str.splitlines() or ['']
		with self._condition:
			self._buffer.extend(lines)
			self._last_update_time = time.monotonic()
			self._condition.notify_all()

	def wait_for_complete(self, total_timeout: float, idle_timeout: float) -> bool:
		"""
		等待输出收敛。

		返回 `True` 表示命中总超时，返回 `False` 表示在静默窗口内自然结束。
		"""
		deadline = time.monotonic() + total_timeout
		with self._condition:
			while True:
				now = time.monotonic()
				if now >= deadline:
					return True

				idle_elapsed = now - self._last_update_time
				if idle_elapsed >= idle_timeout:
					return False

				wait_time = min(deadline - now, idle_timeout - idle_elapsed)
				self._condition.wait(timeout=wait_time)

	def get_output(self) -> List[str]:
		"""读取当前收集到的输出副本。"""
		with self._condition:
			return self._buffer.copy()


class CapturingCommandSource(CommandSource):
	"""用于执行 MCDR 命令的自定义命令源。"""

	def __init__(self, server, collector: CommandOutputCollector):
		self._server = server.as_basic_server_interface()
		self._collector = collector

	def get_server(self):
		return self._server

	def get_permission_level(self) -> int:
		return PermissionLevel.PLUGIN_LEVEL

	def reply(self, message, **kwargs) -> None:
		console_text = kwargs.get('console_text')
		self._collector.add_text(console_text if console_text is not None else message)

	def __str__(self):
		return 'ConsoleCommandApiSource'

	def __repr__(self):
		return self.__str__()


class CommandLogHandler(logging.Handler):
	"""临时挂到 logger 上，用于抓取命令执行期间的控制台日志。"""

	def __init__(self, collector: CommandOutputCollector):
		super().__init__()
		self.collector = collector
		self._seen_record_ids: set[int] = set()
		self._lock = threading.Lock()

	def emit(self, record: logging.LogRecord) -> None:
		record_id = id(record)
		with self._lock:
			if record_id in self._seen_record_ids:
				return
			self._seen_record_ids.add(record_id)
			if len(self._seen_record_ids) > 1024:
				self._seen_record_ids.clear()
				self._seen_record_ids.add(record_id)

		self.collector.add_text(record.getMessage())


class CommandCaptureSession:
	"""管理一次 MCDR 命令执行期间的 source 和日志捕获器。"""

	def __init__(self, server):
		self.collector = CommandOutputCollector()
		self.source = CapturingCommandSource(server, self.collector)
		self._handler = CommandLogHandler(self.collector)
		self._loggers = [logging.getLogger(), server.logger]

	def __enter__(self):
		for logger in self._loggers:
			logger.addHandler(self._handler)
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		for logger in self._loggers:
			logger.removeHandler(self._handler)


class ServerCommandCollector:
	"""用于收集一条 MC 服务端命令的输出。"""

	def __init__(self, request_id: str):
		self.request_id = request_id
		self.collecting = False
		self.buffer: List[str] = []
		self.event = threading.Event()


class ServerCommandCollectorManager:
	"""管理所有正在等待输出的 MC 服务端命令收集器。"""

	def __init__(self):
		self._collectors: Dict[str, ServerCommandCollector] = {}
		self._lock = threading.Lock()

	def create_collector(self, request_id: str) -> ServerCommandCollector:
		collector = ServerCommandCollector(request_id)
		with self._lock:
			self._collectors[request_id] = collector
		return collector

	def remove_collector(self, request_id: str) -> None:
		with self._lock:
			self._collectors.pop(request_id, None)

	def process_server_output(self, raw_content: str) -> None:
		"""在 on_info 中调用，用于把服务端输出路由给对应的收集器。"""
		match = MARKER_PATTERN.search(raw_content)
		if match:
			action, request_id = match.groups()
			with self._lock:
				collector = self._collectors.get(request_id)
				if collector is None:
					return
				if action == 'START':
					collector.collecting = True
				else:
					collector.collecting = False
					collector.event.set()
			return

		with self._lock:
			for collector in self._collectors.values():
				if collector.collecting:
					collector.buffer.append(raw_content)


server_command_collector_manager = ServerCommandCollectorManager()
