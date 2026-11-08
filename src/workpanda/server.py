"""网络层启动装配：绑定地址、TLS 与回退开关。

默认监听 `127.0.0.1` 且不启用 TLS，也就是"不对外暴露"的状态。把服务推向网络
属于安全边界变更，因此这里用三道门把它拦住，缺一道就启动失败，而不是"降级
继续跑"：

1. `bind_host` 不是回环地址；
2. `tls_enabled=true`，且证书与私钥都存在；
3. `allow_network_bind=true`（有人明确为这次暴露负责）。

失败要早：明文把政务服务挂在局域网上不会报任何错，只会安静地把群众数据交出去。

回退同样是配置级的，改回去重启即可：

```bash
WORKPANDA_BIND_HOST=127.0.0.1 WORKPANDA_TLS_ENABLED=false WORKPANDA_ALLOW_NETWORK_BIND=false
```
"""

from __future__ import annotations

import uvicorn

from workpanda.capacity import validate_deployment_topology
from workpanda.config import Settings, get_settings

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class NetworkConfigError(ValueError):
    """网络配置不满足安全前提。启动即失败，不静默降级。"""


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def validate_network_settings(settings: Settings) -> None:
    """校验绑定与 TLS 的组合。只做检查，不碰网络、不读凭据。"""

    if settings.tls_enabled:
        pairs = (
            ("tls_certfile", settings.tls_certfile),
            ("tls_keyfile", settings.tls_keyfile),
        )
        missing = [name for name, value in pairs if value is None]
        if missing:
            raise NetworkConfigError(
                f"tls_enabled=true 时必须同时配置 {' 与 '.join(missing)}；"
                "只开开关不给证书，uvicorn 会退回明文监听。"
            )
        for name, value in (
            ("tls_certfile", settings.tls_certfile),
            ("tls_keyfile", settings.tls_keyfile),
        ):
            assert value is not None  # 上面的 missing 检查已保证
            if not value.is_file():
                raise NetworkConfigError(f"{name} 指向的文件不存在或不是普通文件：{value}")

    if is_loopback(settings.bind_host):
        return

    if not settings.allow_network_bind:
        raise NetworkConfigError(
            f"bind_host={settings.bind_host} 不是回环地址，属于安全边界变更；"
            "确认要暴露时请显式设置 allow_network_bind=true。"
        )
    if not settings.tls_enabled:
        raise NetworkConfigError(
            f"bind_host={settings.bind_host} 对外监听时必须启用 TLS；"
            "明文暴露会让会话令牌与任务内容在网络上是可读的。"
        )


def build_uvicorn_config(settings: Settings | None = None) -> uvicorn.Config:
    """构造 uvicorn 配置。这里只做装配与校验，不启动服务。"""

    resolved = settings or get_settings()
    validate_network_settings(resolved)
    # 部署形态与网络绑定同属启动期安全前提：声明多实例却仍用进程内状态后端时，
    # 不能等 lifespan 建好容器才发现（Issue #53）。
    validate_deployment_topology(resolved)
    options: dict = {
        "host": resolved.bind_host,
        "port": resolved.bind_port,
        "log_level": "info",
    }
    if resolved.tls_enabled:
        options["ssl_certfile"] = str(resolved.tls_certfile)
        options["ssl_keyfile"] = str(resolved.tls_keyfile)
    # 用导入字符串而不是 app 对象：构造配置时不该顺带把应用（及其配置）加载一遍。
    return uvicorn.Config("workpanda.main:app", **options)


def run(settings: Settings | None = None) -> None:
    uvicorn.Server(build_uvicorn_config(settings)).run()
