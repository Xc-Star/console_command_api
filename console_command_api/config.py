"""
插件配置。
"""
import secrets

from mcdreforged.api.all import *


class Config(Serializable):
	"""插件配置模型。"""

	token: str = ''
	timeout: float = 5.0
	idle_timeout: float = 0.2
	host: str = '0.0.0.0'
	port: int = 8000


def generate_token_if_empty(config: Config) -> Config:
	"""首次启动时自动生成鉴权 token。"""
	if not config.token:
		config.token = secrets.token_hex(16)
	return config
