"""
插件入口。

负责加载配置、启动 HTTP 服务，以及在插件卸载时停止服务线程。
"""
import threading

from mcdreforged.api.all import *

from .collector import server_command_collector_manager
from .config import Config, generate_token_if_empty
from .server import set_server_context, start_server, stop_server

PLUGIN_METADATA = ServerInterface.get_instance().as_plugin_server_interface().get_self_metadata()

config = Config.get_default()
server_thread: threading.Thread | None = None


def tr(translation_key: str, *args, **kwargs) -> RTextMCDRTranslation:
	"""读取插件翻译文本。"""
	return ServerInterface.get_instance().rtr(f'{PLUGIN_METADATA.id}.{translation_key}', *args, **kwargs)


def on_info(server: PluginServerInterface, info: Info):
	"""监听服务端输出，为 MC 服务端命令请求收集响应。"""
	if info.is_from_server:
		server_command_collector_manager.process_server_output(info.raw_content)


def on_load(server: PluginServerInterface, prev):
	"""加载插件并启动 HTTP 服务。"""
	global config, server_thread

	config = server.load_config_simple(target_class=Config)
	config = generate_token_if_empty(config)
	server.save_config_simple(config)

	set_server_context(server, config)

	server_thread = threading.Thread(
		target=start_server,
		args=(config.host, config.port),
		daemon=True,
		name='Console Command API - HTTP Server',
	)
	server_thread.start()

	server.logger.info(tr('server_started', config.host, config.port))


def on_unload(server: PluginServerInterface):
	"""卸载插件并停止 HTTP 服务。"""
	global server_thread

	stop_server()
	if server_thread is not None and server_thread.is_alive():
		server_thread.join(timeout=5)
	server_thread = None

	server.logger.info(tr('server_stopped'))
