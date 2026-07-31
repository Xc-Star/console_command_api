"""
插件入口 v2。

负责加载配置、启动 WebSocket 客户端，以及在插件卸载时断开连接。
"""
import threading
from typing import Optional

from mcdreforged.api.all import *

from .collector import server_command_collector_manager
from .config import Config
from .ws_client import health_tracker, set_server_context, set_translator, start_client, stop_client

PLUGIN_METADATA = ServerInterface.get_instance().as_plugin_server_interface().get_self_metadata()

config = Config.get_default()
client_thread: Optional[threading.Thread] = None


def tr(translation_key: str, *args, **kwargs) -> RTextMCDRTranslation:
    """读取插件翻译文本"""
    return ServerInterface.get_instance().rtr(f'{PLUGIN_METADATA.id}.{translation_key}', *args, **kwargs)


def on_info(server: PluginServerInterface, info: Info):
    """监听服务端输出，为 MC 服务端命令请求收集响应"""
    if info.is_from_server:
        server_command_collector_manager.process_server_output(info.raw_content)


def on_load(server: PluginServerInterface, prev):
    """加载插件并启动 WebSocket 客户端"""
    global config, client_thread

    config = server.load_config_simple(target_class=Config)
    set_server_context(server, config)
    set_translator(lambda key: server.rtr(f'{PLUGIN_METADATA.id}.{key}'))

    client_thread = threading.Thread(
        target=start_client,
        args=(config,),
        daemon=True,
        name='Console Command API - WS Client',
    )
    client_thread.start()

    server.logger.info(tr('client_started', config.ws_url, config.server_name))


def on_unload(server: PluginServerInterface):
    """卸载插件并停止 WebSocket 客户端"""
    global client_thread

    stop_client()
    if client_thread is not None and client_thread.is_alive():
        client_thread.join(timeout=5)
    client_thread = None

    server.logger.info(tr('client_stopped'))
