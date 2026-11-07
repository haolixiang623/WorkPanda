"""应用配置。所有环境相关设置集中在此，业务代码不直接读取环境变量。"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

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
    # 单主体旧配置，等价于一个 admin 主体；多主体请改用 `ops_tokens`。
    ops_token: str | None = None
    # 按主体的运维凭据：`主体:角色:密钥`，多个用逗号分隔。
    # 一个主体可以有多个密钥（轮换期间新旧并存），但不同主体的密钥不能重复。
    ops_tokens: str = ""
    # 网络绑定与 TLS。默认只监听回环且不启用 TLS，也就是"不对外暴露"的状态。
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    # 绑定到非回环地址属于安全边界变更，必须显式确认；
    # 未经确认的非回环绑定会在启动时直接失败（见 server.py）。
    allow_network_bind: bool = False
    tls_enabled: bool = False
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    # 允许跨域访问的来源，逗号分隔。不接受任何通配符：预检一旦放行，
    # 浏览器会替任意站点把请求发过来并带上凭据，等于把会话交给全网。
    cors_origins: str = ""
    # 通用 harness：直连 OpenAI 兼容接口，自带工具循环与预算控制。
    # 优先级高于 Octop 运行时：harness 用我们的受控循环，Octop 只当模型后端。
    harness_enabled: bool = False
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str = "gpt-4o-mini"
    model_timeout_seconds: float = 60.0
    # 模型调用的有界重试：只对瞬时失败（429/5xx/传输错误）生效。
    # 0 表示不重试；重试同样受 harness_deadline_seconds 约束。
    model_retries: int = 1
    model_retry_backoff_seconds: float = 0.5
    # 是否沿用系统代理环境变量。内网地址被代理拦截时置为 false。
    model_trust_env: bool = True
    harness_max_steps: int = 6
    harness_max_tool_calls: int = 8
    # 0 表示不限；仅在模型服务上报 usage 时生效。
    harness_max_tokens: int = 0
    harness_deadline_seconds: float = 45.0
    # 可重试失败的额外尝试次数（仅对幂等工具生效）。
    harness_tool_retries: int = 1
    # 同一轮里最多几个工具并发执行。1 表示串行（默认）。
    # 只有整批都是幂等工具、且整批放得进剩余调用预算时才会真的并发；
    # 工具本身必须线程安全，第三方工具有共享状态时保持 1。
    harness_tool_concurrency: int = 1
    # 允许"已超时但仍未退出"的工具线程数上限；触顶后拒绝发起新的工具调用
    # （BLOCKED `tool_capacity`，确定没执行）。超时的线程无法被强制终止，
    # 不设上限时它们会在真实长任务里悄悄堆到耗尽句柄。0 表示关闭这道闸门。
    harness_max_stuck_threads: int = 16
    # 落盘与运维接口的脱敏开关。默认开启；除非有明确理由，否则不要关。
    harness_redact: bool = True
    # 进程内 trace 保留条数上限，避免留痕变成无限增长的内存占用。
    # 0 表示不限（只建议在 sqlite 后端且有人负责清理时使用）。
    harness_trace_capacity: int = 500
    # trace 存储后端：memory（进程内）/ sqlite（与任务库同文件）/ auto（跟随 store_backend）。
    harness_trace_backend: str = "auto"
    # 断点续跑：默认关闭。开启后每个工具调用都会落 checkpoint（含未脱敏的对话原文），
    # 因此必须先确认存储的权限与留存策略可接受。
    harness_resume_enabled: bool = False
    harness_resume_ttl_seconds: float = 900.0
    # 步骤租约：同一 (task, step) 只允许一个执行者。多进程试点时开启；
    # 0 表示按 deadline + 余量自动推导 TTL。
    harness_lease_enabled: bool = False
    harness_lease_ttl_seconds: float = 0.0
    # 取消信号的可见范围：memory（仅本进程，默认）/ sqlite（同机多进程，与任务库同文件）。
    # 跨主机多实例仍需换成共享后端，这里只解决"另一个进程里的执行看不见取消"。
    harness_cancel_backend: str = "memory"
    # 共享取消信号的有效期；过期后不再拦住同一任务的后续执行。
    harness_cancel_ttl_seconds: float = 600.0
    # 退避等待里轮询共享信号的粒度（秒）。太大时跨进程取消会显得"点了没反应"。
    harness_cancel_poll_seconds: float = 0.1
    # 逗号分隔的域名白名单；为空则不注册 http 工具（默认关闭比默认全开安全）。
    harness_allowed_hosts: str = ""
    # 审计归档路径。为空则不归档（默认关闭）。归档只追加、按大小滚动，
    # 不随 trace 容量淘汰，用于事后核对"某次提交到底发出去没有"。
    harness_audit_path: Path | None = None
    harness_audit_max_bytes: int = 5_000_000
    # 滚动保留的备份份数；0 表示只留当前文件。
    harness_audit_backups: int = 3
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

    @property
    def cors_origin_list(self) -> tuple[str, ...]:
        """解析并校验跨域来源白名单。

        配置写错时抛错而不是"默默忽略"：被忽略的那一项会让前端收到一个没有
        `Access-Control-Allow-Origin` 的响应，现象是"接口 200 但浏览器报跨域"，
        比启动失败难查得多。
        """

        origins: list[str] = []
        for raw in self.cors_origins.split(","):
            origin = raw.strip()
            if not origin:
                continue
            if "*" in origin:
                raise ValueError(
                    "cors_origins 不允许通配符：请显式列出每个来源（例如 https://ops.example.gov.cn）。"
                )
            parts = urlsplit(origin)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ValueError(f"cors_origins 条目必须是 http(s) 来源，且不带路径：{origin}")
            if parts.path not in {"", "/"} or parts.query or parts.fragment:
                raise ValueError(f"cors_origins 条目不能带路径、查询串或片段：{origin}")
            if origin not in origins:
                origins.append(origin)
        return tuple(origins)


@lru_cache
def get_settings() -> Settings:
    return Settings()
