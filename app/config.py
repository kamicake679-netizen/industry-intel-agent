"""全局配置模块。

使用 pydantic-settings 加载配置：
1. 系统环境变量优先级最高（兼容魔搭创空间 Secrets 等云端注入）；
2. 本地开发时自动回退读取项目根目录下的 .env 文件。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
DB_PATH = DATA_DIR / "intel.db"


class Settings(BaseSettings):
    """应用全局配置。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    siliconflow_api_key: str = Field(
        default="",
        description="大模型 API Key（默认硅基流动，兼容任意 OpenAI 协议服务）",
    )
    siliconflow_base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="OpenAI 兼容接口 Base URL",
    )
    llm_model: str = Field(
        default="deepseek-ai/DeepSeek-V3",
        description="默认对话与抽取模型名称",
    )
    port: int = Field(
        default=7860,
        ge=1,
        le=65535,
        description="服务监听端口",
    )
    webhook_url: str = Field(
        default="",
        description="简报推送 Webhook 地址（可选）",
    )
    daily_llm_budget: int = Field(
        default=800,
        ge=1,
        description="全局每日 LLM 调用预算上限",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取缓存后的全局配置实例。"""
    return Settings()


settings = get_settings()
