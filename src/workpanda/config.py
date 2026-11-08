"""应用配置。所有环境相关设置集中在此，业务代码不直接读取环境变量。"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from workpanda.domain.deeplink import MAX_TTL_SECONDS
from workpanda.domain.intent_eval import (
    DEFAULT_EVAL_TIMEOUT_SECONDS,
    DEFAULT_MAX_MISS_RATE,
)
from workpanda.domain.intent_sample import MIN_NEAR_MISS_SAMPLES, MIN_POSITIVE_SAMPLES
from workpanda.harness.browser.policy import BrowserPolicy, policy_from_hosts

# 回环主机名单。与 `server.py` 的绑定校验共用一份：两处各写一份，
# 迟早出现"启动检查认为不对外、静态托管却认为对外"的分叉。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _validated_site_root(raw: str, field: str) -> str:
    """校验"站点根"配置：必须是带 scheme 的 http(s) 地址，且不带路径。

    两个配置项（深链落地前缀、工作人员任务页）共用这一份校验。带路径的地址会与
    自身的落地路径拼成 `/confirm/confirm` 或 `/tasks/tasks`，而那种 404 会被当成
    "链接过期"。宁可在启动时就拒绝。
    """

    value = (raw or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(
            f"{field} 必须是带 scheme 的地址（例如 https://wpa.example.com）：{value}"
        )
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError(f"{field} 只能是不带路径的站点根：{value}")
    return value.rstrip("/")


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
    # 卡片深链（N5）的有效期：只用于把浏览器导航到确认页，所以比确认凭证更短。
    # 上限 900 秒——再长就不再是"一次性"，一张留在浏览历史里的卡能被反复点开。
    confirmation_link_ttl_seconds: int = 300
    # 卡片按钮的落地地址（形如 https://wpa.example.com，不带路径）。
    # 留空时接口只返回相对路径：服务端不猜自己的对外域名。
    confirmation_link_base_url: str = ""
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
    # 工作人员凭据：`主体:staff:密钥`，多个用逗号分隔。与 ops / admin 完全独立，
    # 角色只接受 `staff`——工作人员的"能对哪些流程做什么动作"不写在凭据里，
    # 而是维护在工作人员目录（`/api/admin/identities/staff/*`）中，改授权不必换凭据。
    # 未配置时 /api/staff/* 整体不可用（404），不接受 ops / admin 凭据代用。
    staff_tokens: str = ""
    # 企业微信网关（N4）。三项回调配置必须同时给出，缺一项启动即失败：
    # 半套配置会让接口看起来开着、实际每条回调都失败，而那看起来像"对方配错了"。
    # token 与 EncodingAESKey 来自企业微信后台的回调配置，corp_id 是企业 ID——
    # 它同时用于校验解密后的 receive_id（挡住"发给别家企业微信的包"）。
    wecom_callback_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_corp_id: str = ""
    # 出站发卡片需要的应用凭据。留空表示"只接入站、不发卡片"：投递会被记成
    # skipped 而不是 failed，因为那不是故障，是没启用。
    wecom_corp_secret: str = ""
    wecom_agent_id: int = 0
    wecom_timeout_seconds: float = 5.0
    # 工作人员端任务页面的站点根（形如 https://wpa.example.com，不带路径）。
    # 卡片里的深链由它拼出；留空则不发出站卡片——服务端不猜自己的对外域名，
    # 猜错的后果是卡片指向一个不存在的站点，而排障会先怀疑代码。
    staff_console_base_url: str = ""
    # 出站卡片队列容量。满了就丢卡片（只记日志），绝不阻塞任务推进。
    im_card_queue_capacity: int = 200
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
    # 群众侧页面（仓库里的 `web/`）的静态托管。
    #
    # 默认开启，但**只在绑定回环地址时真正挂载**：这个页面会给任何打开它的人
    # 签发会话，而本部署还没有面向公网的认证与 TLS。把它挂到 0.0.0.0 上，等于
    # 让局域网里任何一台机器都能新建任务、占用执行预算。
    #
    # 对外提供服务前先补网关与认证，再显式打开 `web_ui_expose_public`
    # （同时会触发 server.py 的对外绑定三道门）。
    web_ui_enabled: bool = True
    # 静态目录。留空时按仓库布局取 `web/`；安装成 wheel 后那里没有这个目录，
    # 就不挂载（静态资源本就该由网关/独立域名下发，参见 `admin-ui/README.md`）。
    web_ui_dir: Path | None = None
    web_ui_expose_public: bool = False
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
    # ---- 浏览器执行器（P2）----
    # 总开关。关着的时候一个 browser 工具都不注册，模型看不到它们。
    harness_browser_enabled: bool = False
    # 浏览器白名单，**与 http 白名单分开**：浏览器要跨过站，名单通常更长；
    # 共用一份会逼着运维把 http 的抓取范围一起放大。
    # 支持 `host`、`host:port`、`.suffix`（后缀）三种写法。
    harness_browser_allowed_hosts: str = ""
    # 允许点击的提交类控件（按 data-testid）。默认空：任何提交类控件都拒绝。
    # 登录按钮要进去只能在这里逐个登记——登记即代表"人确认过它只建立会话"。
    harness_browser_permitted_click_testids: str = ""
    harness_browser_headless: bool = True
    # 导航与找元素的时限（毫秒）。超时是**明确失败**，不重试。
    harness_browser_navigation_timeout_ms: int = 15_000
    harness_browser_element_timeout_ms: int = 6_000
    # 会话空闲回收（秒）。任务结束没有回调，只能按"多久没人用过"回收。
    harness_browser_session_ttl_seconds: float = 600.0
    # 单次读回给模型的字符上限。
    harness_browser_read_chars: int = 4_000
    # 失败现场（DOM/截图）落盘目录。为空表示不留档——默认不留，
    # 因为留档本身就是在磁盘上多写一份群众数据。
    harness_browser_artifact_dir: Path | None = None
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
    def bind_is_loopback(self) -> bool:
        return self.bind_host.strip().lower() in LOOPBACK_HOSTS

    @property
    def web_ui_served(self) -> bool:
        """群众侧页面是否真的挂载。

        把一个"功能开关"和一条安全前提写在一起，是因为它们只在这条前提下
        才有意义：页面自己会签发会话，对外可达就等于把会话发给所有人。
        """

        if not self.web_ui_enabled:
            return False
        return self.bind_is_loopback or self.web_ui_expose_public

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return tuple(
            host.strip() for host in self.harness_allowed_hosts.split(",") if host.strip()
        )

    @property
    def browser_policy(self) -> BrowserPolicy | None:
        """浏览器工具的策略。开关关着或没配白名单时返回 None（=不注册）。

        三个条件必须同时成立才有浏览器能力：显式打开开关、配了白名单、
        装了可选依赖（后者在第一次真正打开页面时才检查，见 `session`）。
        """

        if not self.harness_browser_enabled:
            return None
        hosts = tuple(
            host.strip()
            for host in self.harness_browser_allowed_hosts.split(",")
            if host.strip()
        )
        if not hosts:
            return None
        permitted = frozenset(
            item.strip()
            for item in self.harness_browser_permitted_click_testids.split(",")
            if item.strip()
        )
        return policy_from_hosts(
            hosts,
            permitted_submit_testids=permitted,
            navigation_timeout_ms=self.harness_browser_navigation_timeout_ms,
            element_timeout_ms=self.harness_browser_element_timeout_ms,
            max_read_chars=self.harness_browser_read_chars,
            session_ttl_seconds=int(self.harness_browser_session_ttl_seconds),
            headless=self.harness_browser_headless,
            artifact_dir=self.harness_browser_artifact_dir,
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

    @property
    def confirmation_link_base(self) -> str:
        """卡片深链的落地前缀，校验后不带结尾斜杠。

        与 `cors_origins` 同一套严格度：写成 `wpa.example.com`（漏了 scheme）或
        带了路径时启动就失败。否则卡片会指向一个打不开的地址，而排障会先从
        "深链过期了吗"开始查——那是最难查的一类问题。
        """

        return _validated_site_root(self.confirmation_link_base_url, "confirmation_link_base_url")

    @property
    def staff_console_base(self) -> str:
        """工作人员端任务页面的站点根。校验规则与深链前缀完全一致。

        两个地址分开配置而不是共用一个：深链落到确认页，卡片落到任务页，
        它们未来可能不是同一个站点（确认页要对外、任务页只在办公网）。
        """

        return _validated_site_root(self.staff_console_base_url, "staff_console_base_url")

    @field_validator("confirmation_link_ttl_seconds")
    @classmethod
    def _check_link_ttl(cls, value: int) -> int:
        # 上限与 domain.deeplink.MAX_TTL_SECONDS 同一个理由：深链只是一次导航，
        # 有效期越长，"一次性"就越容易被浏览历史稀释成"随时可点"。
        if value <= 0 or value > MAX_TTL_SECONDS:
            raise ValueError(
                f"confirmation_link_ttl_seconds 必须在 1..{MAX_TTL_SECONDS} 秒之间：{value}"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
