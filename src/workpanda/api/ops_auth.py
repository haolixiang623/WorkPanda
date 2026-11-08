"""运维接口鉴权：主体、角色与凭据轮换。

这里刻意不做"一个共享口令"。共享口令有两个解决不了的问题：撤销某个人要停掉
所有人的访问；事后也答不出"这次查看是谁做的"。所以运维凭据按主体发放，每个
主体带一个角色，角色决定能看见多少运维数据。

角色只有两档，够用且不容易配错：

- `viewer`：业务概览——任务列表与留痕聚合指标。用于日常值班看"有没有异常"。
- `admin`：明细——任务详情、事件流、留痕原文、审计归档、工具线程台账。
  明细里包含槽位内容与对外调用的原文，可见范围就是数据范围，只有需要排障的人配。

轮换靠"同一主体允许有多个密钥"实现：先加新密钥、把调用方切过去、再删旧密钥，
中间不会出现谁也用不了的空窗。不同主体之间禁止共用同一个密钥——共用之后，
任何一次事故都无法定位到人。
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass

ADMIN = "admin"
VIEWER = "viewer"
ROLES = (ADMIN, VIEWER)

# 密钥短于这个长度时，口令空间已经小到可以被在线枚举。
MIN_SECRET_LENGTH = 16
_SUBJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class OpsAccessConfigError(ValueError):
    """运维凭据配置非法。配置写错时启动即失败，绝不静默忽略一条写错的凭据。"""


class OpsUnauthenticated(Exception):
    """未提供凭据，或凭据不匹配任何主体。"""


class OpsDisabled(Exception):
    """未配置任何运维凭据，接口按不存在处理。"""


@dataclass(frozen=True)
class OpsPrincipal:
    """一个已通过鉴权的运维主体。`subject` 只用于归属与排障，不是实名身份。"""

    subject: str
    role: str = ADMIN

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


@dataclass(frozen=True)
class _Credential:
    principal: OpsPrincipal
    secret: bytes


class OpsAccessPolicy:
    """凭据集合与角色判定。构造后不可变，可安全地被多请求共享。"""

    def __init__(self, credentials: list[_Credential]) -> None:
        self._credentials = tuple(credentials)

    @property
    def enabled(self) -> bool:
        return bool(self._credentials)

    @property
    def subjects(self) -> tuple[str, ...]:
        """已发放凭据的主体（不含密钥），用于运维自查"谁还能进来"。"""

        return tuple(sorted({credential.principal.subject for credential in self._credentials}))

    def authenticate(self, presented: str | None) -> OpsPrincipal:
        """把请求头里的凭据换成主体。

        逐个比较、不提前跳出：一旦"命中第一个就 return"，响应时间就会随密钥
        前缀匹配长度变化，等于给了攻击者一个逐字节试密钥的信道。
        """

        if not self._credentials:
            raise OpsDisabled
        if not presented:
            raise OpsUnauthenticated
        supplied = presented.encode("utf-8")
        matched: OpsPrincipal | None = None
        for credential in self._credentials:
            if hmac.compare_digest(supplied, credential.secret):
                matched = credential.principal
        if matched is None:
            raise OpsUnauthenticated
        return matched


def _parse_entry(
    item: str, *, allowed_roles: tuple[str, ...] = ROLES, field: str = "ops_tokens"
) -> _Credential:
    parts = item.split(":", 2)
    if len(parts) != 3:
        raise OpsAccessConfigError(
            f"{field} 每项必须是 主体:角色:密钥（角色取 {' 或 '.join(allowed_roles)}）。"
        )
    subject, role, secret = (part.strip() for part in parts)
    if not _SUBJECT_PATTERN.match(subject):
        raise OpsAccessConfigError(
            f"{field} 主体名非法：{subject!r}，只允许字母、数字、点、下划线和连字符。"
        )
    if role not in allowed_roles:
        raise OpsAccessConfigError(
            f"{field} 角色非法：{role!r}，只允许 {' 或 '.join(allowed_roles)}。"
        )
    if len(secret) < MIN_SECRET_LENGTH:
        raise OpsAccessConfigError(
            f"{field} 中主体 {subject!r} 的密钥过短，至少 {MIN_SECRET_LENGTH} 个字符。"
        )
    return _Credential(principal=OpsPrincipal(subject=subject, role=role), secret=secret.encode())


def build_ops_policy(
    ops_token: str | None,
    ops_tokens: str,
    *,
    allowed_roles: tuple[str, ...] = ROLES,
    field: str = "ops_tokens",
    legacy_subject: str = "legacy",
    legacy_role: str = ADMIN,
) -> OpsAccessPolicy:
    """从配置构建凭据集合。

    `ops_token` 是单主体旧配置，按 admin 处理，主体名固定为 `legacy`：它没有
    主体区分能力，保留它只是为了不打断既有试点部署，新部署应改用 `ops_tokens`。

    `allowed_roles` / `field` 让同一套解析逻辑也能服务工作人员凭据
    （`WORKPANDA_STAFF_TOKENS`，角色只允许 `staff`）：三种凭据的格式、长度下限、
    重复密钥禁令必须完全一致，各写一份迟早会漂移出"某一类凭据的密钥可以更短"。
    """

    credentials: list[_Credential] = []
    seen_secrets: dict[bytes, str] = {}
    roles_by_subject: dict[str, str] = {}

    if ops_token:
        secret = ops_token.strip()
        if len(secret) < MIN_SECRET_LENGTH:
            raise OpsAccessConfigError(
                f"ops_token 过短，至少 {MIN_SECRET_LENGTH} 个字符；"
                "如果只是本地自测，也请生成一个随机串。"
            )
        credentials.append(
            _Credential(
                principal=OpsPrincipal(subject=legacy_subject, role=legacy_role),
                secret=secret.encode(),
            )
        )
        seen_secrets[secret.encode()] = legacy_subject
        roles_by_subject[legacy_subject] = legacy_role

    for item in ops_tokens.replace("\n", ",").split(","):
        if not item.strip():
            continue
        credential = _parse_entry(item, allowed_roles=allowed_roles, field=field)
        subject = credential.principal.subject
        known_role = roles_by_subject.get(subject)
        if known_role is not None and known_role != credential.principal.role:
            raise OpsAccessConfigError(
                f"主体 {subject!r} 同时配置了 {known_role} 与 "
                f"{credential.principal.role} 两个角色；"
                "同一主体只能有一个角色，否则权限边界会随命中哪条凭据而变。"
            )
        owner = seen_secrets.get(credential.secret)
        if owner is not None:
            raise OpsAccessConfigError(
                f"主体 {subject!r} 与 {owner!r} 使用了同一个密钥；不同主体必须各自持有独立密钥。"
            )
        roles_by_subject[subject] = credential.principal.role
        seen_secrets[credential.secret] = subject
        credentials.append(credential)

    return OpsAccessPolicy(credentials)
