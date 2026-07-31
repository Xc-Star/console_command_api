"""
插件配置 v2。
"""
from mcdreforged.api.all import *


class Config(Serializable):
    """插件配置模型 v2"""

    token: str = ''
    timeout: float = 5.0
    idle_timeout: float = 0.2
    ws_url: str = 'ws://127.0.0.1:8001/ws'
    server_name: str = 'default'
    auto_reconnect: bool = True
    reconnect_interval: float = 5.0
