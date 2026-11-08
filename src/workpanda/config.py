"""应用配置。所有环境相关设置集中在此，业务代码不直接读取环境变量。"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

from workpanda.domain.intent_eval import (
    DEFAULT_EVAL_TIMEOUT_SECONDS,
    DEFAULT_MAX_MISS_RATE,
)
from workpanda.domain.intent_sample import MIN_NEAR_MISS_SAMPLES, MIN_POSITIVE_SAMPLES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKPANDA_", env_file=".env", extra="ignore")

    app_name: str = "WorkPanda"
    # memory：仅进程内，重启丢失；sqlite：本地持久化。生产应接入 PostgreSQL。
    store_backend: str = "memory"
    sqlite_path: Path = Path("workpanda.db")
    # 运维对「本次部署会同时运行几个实例」的显式声明，不是运行时探测值：
    # 服务无法自己数出有几个进程，只能由部署方声明。默认 1 = 当前 launchd 单实例试点。
    # 声明 >1 时由 capacity.validate_deployment_topology 在启动期强制要求跨进程共享
    # 状态后端与步骤租约，避免每个实例都以为自己是唯一执行者（Issue #53）。
    # 阈值、升级顺序与回滚见 docs/CAPACITY-RUNBOOK.md。
    expected_instances: int = 1
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
    # 管理侧 SOP 配置台凭据，格式与 ops_tokens 相同，但**独立发放**：
    # 运维只读凭据能看任务明细，不能改流程的确认语义与工具白名单。
    # 未配置时 /api/admin/* 整体不可用（404），不接受 ops_tokens 代用。
    admin_tokens: str = ""
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
    # 压测实测（docs/HARNESS.md 第 21 节）：每条卡住线程约 41KB 峰值 RSS，
    # 持连接时约 2 个句柄，16 条合计 <1MB / 32 句柄——离资源边界很远，所以这个
    # 上限的语义是"工具已经坏了"的停止信号，不是资源天花板，不必因压测调大。
    # 闸门数的是已放弃的线程，并发 C 时最坏会到「上限 + C - 1」，按资源估算用它。
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
    # 0 表示按 deadline + 120s 自动推导（余量取自"模型超时 60s × 含一次重试"，
    # 因为已发出的模型调用不受 deadline 约束，docs/HARNESS.md 第 21 节压测过）。
    # 自动值只是兜底、没有剩余空间：默认配置下正好 165s 对上最坏 165s。
    # 多进程试点请显式设 180。
    harness_lease_enabled: bool = False
    harness_lease_ttl_seconds: float = 0.0
    # 取消信号的可见范围：memory（仅本进程，默认）/ sqlite（同机多进程，与任务库同文件）
    # / redis（跨主机多实例）。选 redis 时租约与预算账本一并走共享后端。
    harness_cancel_backend: str = "memory"
    # 共享状态后端的总开关。留空表示沿用 `harness_cancel_backend`（向后兼容），
    # 取值 memory / sqlite / redis。写 redis 时租约、取消信号、预算账本三处同时共享。
    harness_shared_backend: str = ""
    # redis 连接串：`redis://[:密码@]主机:端口/库号`。未配置而选了 redis 时启动直接报错。
    # 只允许内网明文连接：这里是状态面（不含业务数据），但仍不应暴露在公网。
    harness_redis_url: str | None = None
    # 单条共享状态命令的超时。取消轮询在退避等待里每 0.1s 一次，超时必须远小于它，
    # 否则"点了取消没反应"；超时按 fail-closed 处理，不会静默续跑。
    harness_redis_timeout_seconds: float = 0.5
    # 键前缀。同一个 Redis 被多套环境共用时，靠它区分，避免串状态。
    harness_shared_key_prefix: str = "workpanda"
    # 预算账本 TTL；0 表示按 deadline + 120s 自动推导（与租约同一口径）。
    # TTL 必须长于一次执行的最坏耗时，否则执行中途账本过期等于预算被重置。
    harness_budget_ttl_seconds: float = 0.0
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
    # 骨架自检流程，只用于验证任务链路；正式接入业务前必须关闭。
    enable_demo_flow: bool = True
    # 发布门禁：该版本写了回放用例时，回放未通过不得发布。
    # 默认开启——关掉它意味着"用例只是摆设"，而这恰好是最难被发现的失效方式；
    # 没写用例的版本不受影响（这条门禁不假装覆盖没有基线的流程）。
    replay_release_gate: bool = True
    # 单条用例的回放时限。回放不碰网络，正常在毫秒级完成；超时说明流程不收敛或
    # 代码有问题，此时判"无结论"而不是继续等。
    replay_timeout_seconds: float = 10.0
    # 识别样例门禁：发布前必须带够正例与近似误例，且评测里不得出现误路由。
    # 默认开启——关掉它意味着"样例只是摆设"，而这恰好是最难被发现的失效方式：
    # 路由覆盖度下降不会报错，只会表现为"澄清率上升"。
    intent_sample_gate: bool = True
    # 发布所需的最少样例条数（正例 / 近似误例）。下限取自骨架自检流程的基线，
    # 与 `domain/intent_sample.py` 的常量同源：门禁与 CI 用同一套数字。
    intent_eval_min_positive: int = MIN_POSITIVE_SAMPLES
    intent_eval_min_near_miss: int = MIN_NEAR_MISS_SAMPLES
    # 漏路由率上限（0 表示一条都不许漏）。误路由没有开关：必须为 0。
    intent_eval_max_miss_rate: float = DEFAULT_MAX_MISS_RATE
    # 一次评测的时限。评测是纯函数、正常在毫秒级完成；超时说明规则实现卡死，
    # 此时判"无结论"而不是继续等（等下去 CI 会挂住、门禁会静默放行）。
    intent_eval_timeout_seconds: float = DEFAULT_EVAL_TIMEOUT_SECONDS

    @property
    def shared_backend(self) -> str:
        """实际生效的共享状态后端。

        `harness_shared_backend` 优先，留空时沿用旧的 `harness_cancel_backend`：
        老部署只改一个环境变量就能把租约、取消、预算一起切到共享后端，而不用
        同时维护两套开关。
        """

        chosen = (self.harness_shared_backend or self.harness_cancel_backend or "memory").strip()
        return chosen.lower() or "memory"

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
