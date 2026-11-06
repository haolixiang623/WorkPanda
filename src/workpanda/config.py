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
    # 运维只读接口凭据（供 Octop 控制台使用）。未配置时接口整体不可用。
    ops_token: str | None = None
    # 通用 harness：直连 OpenAI 兼容接口，自带工具循环与预算控制。
    # 优先级高于 Octop 运行时：harness 用我们的受控循环，Octop 只当模型后端。
    harness_enabled: bool = False
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str = "gpt-4o-mini"
    model_timeout_seconds: float = 60.0
    # 是否沿用系统代理环境变量。内网地址被代理拦截时置为 false。
    model_trust_env: bool = True
    harness_max_steps: int = 6
    harness_max_tool_calls: int = 8
    harness_deadline_seconds: float = 45.0
    # 逗号分隔的域名白名单；为空则不注册 http 工具（默认关闭比默认全开安全）。
    harness_allowed_hosts: str = ""
    # Octop-Harness 运行时。默认关闭，未配置模型凭据时不启用。
    octop_enabled: bool = False
    octop_agent_name: str = "workpanda"
    octop_model: str = "gpt-4o-mini"
    octop_provider: str = "openai"
    octop_api_key: str | None = None
    octop_base_url: str | None = None
    octop_workspace: str | None = None
    octop_timeout_seconds: float = 30.0
    # 骨架自检流程，只用于验证任务链路；正式接入业务前必须关闭。
    enable_demo_flow: bool = True

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return tuple(
            host.strip() for host in self.harness_allowed_hosts.split(",") if host.strip()
        )

    @property
    def model_configured(self) -> bool:
        return bool(self.model_base_url and self.model_api_key and self.model_name)


@lru_cache
def get_settings() -> Settings:
    return Settings()
