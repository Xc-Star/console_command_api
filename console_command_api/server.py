"""
HTTP 服务模块。

这里只暴露两个接口：
1. `/health`：健康检查
2. `/execute`：执行命令并返回输出

规则：
- `!!` 开头的命令按 MCDR 命令执行
- 其他命令按 MC 服务端命令执行
"""
import asyncio
import time
import uuid
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .collector import (
	CommandCaptureSession,
	command_execution_lock,
	server_command_collector_manager,
)

app = FastAPI(title='Console Command API', version='1.0.0')
security = HTTPBearer()

_config = None
_server_interface = None
_uvicorn_server: Optional[uvicorn.Server] = None


class ExecuteRequest(BaseModel):
	"""执行命令请求体。"""

	command: str


class ExecuteResult(BaseModel):
	"""命令执行结果。"""

	request_id: str
	command: str
	command_type: str
	output: list[str]
	output_text: str
	timed_out: Optional[bool] = None


class ApiResponse(BaseModel):
	"""统一响应结构。"""

	code: int
	data: Any = None
	msg: str


def success_response(data: Any = None, msg: str = 'success') -> ApiResponse:
	return ApiResponse(code=200, msg=msg, data=data)


def error_response(code: int, msg: str, data: Any = None) -> ApiResponse:
	return ApiResponse(code=code, msg=msg, data=data)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
	return JSONResponse(
		status_code=exc.status_code,
		content=error_response(exc.status_code, str(exc.detail)).model_dump(),
	)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
	return JSONResponse(
		status_code=500,
		content=error_response(500, str(exc)).model_dump(),
	)


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
	"""校验 HTTP Bearer Token。"""
	if _config is None:
		raise HTTPException(status_code=500, detail='Server not initialized')
	if credentials.credentials != _config.token:
		raise HTTPException(status_code=401, detail='Invalid authentication token')
	return credentials.credentials


def _build_execute_result(
	request_id: str,
	command: str,
	command_type: str,
	output: list[str],
	timed_out: Optional[bool] = None,
) -> ExecuteResult:
	return ExecuteResult(
		request_id=request_id,
		command=command,
		command_type=command_type,
		output=output,
		output_text='\n'.join(output),
		timed_out=timed_out,
	)


def _execute_mcdr_command(command: str, request_id: str) -> ApiResponse:
	"""执行 MCDR 命令并收集 source.reply 和控制台日志。"""
	with CommandCaptureSession(_server_interface) as session:
		try:
			_server_interface.execute_command(command, source=session.source)
			timed_out = session.collector.wait_for_complete(
				total_timeout=_config.timeout,
				idle_timeout=_config.idle_timeout,
			)
			result = _build_execute_result(
				request_id=request_id,
				command=command,
				command_type='mcdr',
				output=session.collector.get_output(),
				timed_out=timed_out,
			)
			return success_response(result.model_dump())
		except Exception as e:
			result = _build_execute_result(
				request_id=request_id,
				command=command,
				command_type='mcdr',
				output=session.collector.get_output(),
			)
			return error_response(500, str(e), result.model_dump())


def _execute_server_command(command: str, request_id: str) -> ApiResponse:
	"""执行 MC 服务端命令并从服务端输出中截取响应。"""
	if not _server_interface.is_server_running():
		return error_response(503, 'Minecraft server is not running')

	collector = server_command_collector_manager.create_collector(request_id)
	try:
		_server_interface.execute(f'say [CMD_API] START {request_id}')
		time.sleep(0.1)
		_server_interface.execute(command)
		time.sleep(0.1)
		_server_interface.execute(f'say [CMD_API] END {request_id}')

		timed_out = not collector.event.wait(timeout=_config.timeout)
		result = _build_execute_result(
			request_id=request_id,
			command=command,
			command_type='minecraft',
			output=collector.buffer.copy(),
			timed_out=timed_out,
		)
		return success_response(result.model_dump())
	except Exception as e:
		result = _build_execute_result(
			request_id=request_id,
			command=command,
			command_type='minecraft',
			output=collector.buffer.copy(),
		)
		return error_response(500, str(e), result.model_dump())
	finally:
		server_command_collector_manager.remove_collector(request_id)


def _execute_command(command: str) -> ApiResponse:
	"""根据命令前缀选择执行 MCDR 命令或 MC 服务端命令。"""
	if _server_interface is None or _config is None:
		return error_response(500, 'Plugin not initialized')

	command = command.strip()
	if not command:
		return error_response(400, 'Command cannot be empty')

	request_id = str(uuid.uuid4())

	with command_execution_lock:
		if command.startswith('!!'):
			return _execute_mcdr_command(command, request_id)
		return _execute_server_command(command, request_id)


@app.post('/execute', response_model=ApiResponse)
async def execute_command(request: ExecuteRequest, _token: str = Security(verify_token)):
	"""执行命令。"""
	return await asyncio.to_thread(_execute_command, request.command)


@app.get('/health', response_model=ApiResponse)
async def health_check():
	"""返回插件和 MCDR 服务的基本状态。"""
	data = {
		'status': 'ok',
		'server_running': _server_interface.is_server_running() if _server_interface else False,
	}
	return success_response(data)


def set_server_context(server_interface, config):
	"""注入 MCDR 的服务接口和插件配置。"""
	global _server_interface, _config
	_server_interface = server_interface
	_config = config


def start_server(host: str, port: int):
	"""启动 Uvicorn HTTP 服务。"""
	global _uvicorn_server
	uvicorn_config = uvicorn.Config(app, host=host, port=port, log_level='warning')
	_uvicorn_server = uvicorn.Server(uvicorn_config)
	try:
		_uvicorn_server.run()
	finally:
		_uvicorn_server = None


def stop_server():
	"""停止 Uvicorn HTTP 服务。"""
	if _uvicorn_server is not None:
		_uvicorn_server.should_exit = True
