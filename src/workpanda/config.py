"""应用配置。所有环境相关设置集中在此，业务代码不直接读取环境变量。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKPANDA_", env_file=".env", extra="ignore")

    app_name: str = "WorkPanda"
    # memory：仅进程内，重启丢失；sqlite：本地持久化。生产应接入 PostgreSQL。
    store_backend: str = "memory"
    sqlite_path: Path = Path("workpanda.db")
    intent_confidence_threshold: float = 0.6
    confirmation_ttl_seconds: int = 300
    event_keepalive_seconds: float = 15.0
    # 会话令牌签名密钥。未配置时使用进程级随机值，重启后旧会话失效。
    session_secret: str | None = None
    session_ttl_seconds: int = 43_200
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30
    # 骨架自检流程，只用于验证任务链路；正式接入业务前必须关闭。
    enable_demo_flow: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
