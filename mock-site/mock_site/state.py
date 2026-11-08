"""模拟站的内存状态：登录、二维码、跨站票据、办理草稿与回执。

一个进程里两个 app 共用同一份 `MockState`。这不是偷懒：账号站与办理站在现实里属于
同一个「服务提供方」，只是不同 origin；共用一份状态才能让「跨 origin 跳转」只跨
origin，而不跨一堵假墙。

状态全在内存里，`reset()` 就等价于重启。没有数据库、没有锁文件、没有后台线程。
每个会改状态的公开方法都在锁内跑，因为 uvicorn 的默认线程池会把同步路径放在
不同线程上；这点并发真实存在，不该靠「反正只有我一个人跑」糊过去。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from mock_site.config import MockConfig, MockConfigError

CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXY23456789"
PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")
SWITCH_FIELDS = (
    "captcha_max_failures",
    "max_attachments",
    "max_upload_bytes",
    "min_attachments",
    "qr_ttl_seconds",
    "submit_delay_seconds",
    "submit_hang_seconds",
    "submit_mode",
)


class MockDomainError(Exception):
    """模拟站的业务失败。`status` 是返回给调用方的 HTTP 状态码。"""

    def __init__(self, status: int, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    summary: str
    pilot: bool = False


# 5 项，其中 1 项是首期试点事项。试点标记只影响页面上的 `data-testid`，不改流程。
SERVICES: tuple[Service, ...] = (
    Service("residence-permit", "居住证签注", "每年签注一次，需本人办理"),
    Service("social-card", "社会保障卡申领", "首次申领与补换卡"),
    Service("elderly-subsidy", "高龄补贴资格核验", "试点事项：跨站办理、三个 tab、多附件",
            pilot=True),
    Service("housing-proof", "公积金缴存证明", "开具近 12 个月缴存证明"),
    Service("water-account", "自来水过户", "房产过户后的水表账户变更"),
)
PILOT_SERVICE = next(item for item in SERVICES if item.pilot)

DOMICILE_TYPES: tuple[tuple[str, str], ...] = (
    ("local", "本市户籍"),
    ("province", "本省非本市户籍"),
    ("outside", "省外户籍"),
)
SITUATIONS: tuple[tuple[str, str], ...] = (
    ("first", "首次申请"),
    ("change", "变更申请"),
    ("reissue", "补发"),
)
_DOMICILE_CODES = {code for code, _ in DOMICILE_TYPES}
_SITUATION_CODES = {code for code, _ in SITUATIONS}


def service_by_id(service_id: str) -> Service:
    for service in SERVICES:
        if service.id == service_id:
            return service
    raise MockDomainError(404, "service_not_found", f"没有这个事项：{service_id}")


def _token(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_hex(8)}"


@dataclass
class Captcha:
    code: str
    created_at: float


@dataclass
class LoginSession:
    sid: str
    username: str
    created_at: float


@dataclass
class QrTicket:
    ticket: str
    created_at: float
    confirmed_at: float | None = None
    consumed_at: float | None = None
    username: str = ""


@dataclass
class HandoffTicket:
    ticket: str
    service_id: str
    username: str
    created_at: float
    used_at: float | None = None


@dataclass
class Attachment:
    file_id: str
    filename: str
    content_type: str
    size: int


@dataclass
class ApplyDraft:
    domicile_type: str | None = None
    situations: list[str] = field(default_factory=list)
    phone: str = ""
    address: str = ""
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class ApplySession:
    sid: str
    username: str
    service_id: str
    created_at: float
    draft: ApplyDraft = field(default_factory=ApplyDraft)


@dataclass(frozen=True)
class Receipt:
    no: str
    service_id: str
    username: str
    domicile_type: str
    situations: tuple[str, ...]
    attachments: int
    created_at: float
    status: str = "accepted"

    def as_dict(self) -> dict[str, object]:
        return {
            "receipt_no": self.no,
            "service_id": self.service_id,
            "service_name": service_by_id(self.service_id).name,
            "username": self.username,
            "domicile_type": self.domicile_type,
            "situations": list(self.situations),
            "attachments": self.attachments,
            "status": self.status,
            "created_at": self.created_at,
        }


class MockState:
    """两个 app 共享的全部状态。构造一次，注入两个 app。"""

    def __init__(
        self,
        config: MockConfig | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._initial_config = config or MockConfig()
        self.config = self._initial_config
        self._clock = clock
        self._lock = threading.RLock()
        self._captchas: dict[str, Captcha] = {}
        self._login_sessions: dict[str, LoginSession] = {}
        self._qr_tickets: dict[str, QrTicket] = {}
        self._handoffs: dict[str, HandoffTicket] = {}
        self._apply_sessions: dict[str, ApplySession] = {}
        self._receipts: dict[str, Receipt] = {}
        self._failures: dict[str, int] = {}
        self._locked_accounts: set[str] = set()
        self._receipt_seq = 0

    # ------------------------------------------------------------------ 测试与运维

    def reset(self) -> None:
        """清空全部状态并把开关恢复成启动时的值。"""

        with self._lock:
            self.config = self._initial_config
            self._captchas.clear()
            self._login_sessions.clear()
            self._qr_tickets.clear()
            self._handoffs.clear()
            self._apply_sessions.clear()
            self._receipts.clear()
            self._failures.clear()
            self._locked_accounts.clear()
            self._receipt_seq = 0

    def patch_switches(self, values: Mapping[str, object]) -> dict[str, object]:
        """运行期改开关。只接受 `SWITCH_FIELDS` 里的键，取值校验与启动时同一套。

        这个端点只给自动化测试用：它让「同一个进程里先跑正常路径、再翻成失败路径」
        成为可能，否则每个负例都要重启一次服务。
        """

        unknown = sorted(set(values) - set(SWITCH_FIELDS))
        if unknown:
            raise MockDomainError(
                400,
                "unknown_switch",
                f"不认识的开关：{', '.join(unknown)}",
                detail="unknown_switch",
            )
        coerced: dict[str, object] = {}
        for key, raw in values.items():
            try:
                if key == "submit_mode":
                    coerced[key] = str(raw)
                elif key in {"captcha_max_failures", "max_attachments", "min_attachments",
                             "max_upload_bytes"}:
                    coerced[key] = int(raw)  # type: ignore[arg-type]
                else:
                    coerced[key] = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise MockDomainError(
                    400, "invalid_switch", f"{key} 的取值不合法：{raw!r}"
                ) from exc
        with self._lock:
            try:
                updated = replace(self.config, **coerced)
            except MockConfigError as exc:
                raise MockDomainError(400, "invalid_switch", str(exc)) from exc
            self.config = updated
            return {key: getattr(updated, key) for key in SWITCH_FIELDS}

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "login_sessions": len(self._login_sessions),
                "apply_sessions": len(self._apply_sessions),
                "qr_tickets": len(self._qr_tickets),
                "handoffs": len(self._handoffs),
                "receipts": len(self._receipts),
                "locked_accounts": sorted(self._locked_accounts),
                "switches": {key: getattr(self.config, key) for key in SWITCH_FIELDS},
            }

    # ------------------------------------------------------------------ 账号站

    def issue_captcha(self) -> tuple[str, str]:
        captcha_id = _token("cap_")
        code = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(4))
        with self._lock:
            self._captchas[captcha_id] = Captcha(code=code, created_at=self._clock())
            while len(self._captchas) > 500:
                self._captchas.pop(next(iter(self._captchas)))
        return captcha_id, code

    def peek_captcha(self, captcha_id: str) -> str:
        """给测试读取答案。页面上不暴露验证码文本，扫码/识别由执行器自己负责。"""

        with self._lock:
            captcha = self._captchas.get(captcha_id)
            if captcha is None:
                raise MockDomainError(404, "captcha_not_found", "验证码不存在或已被使用")
            return captcha.code

    def login(self, *, username: str, password: str, captcha_id: str,
              captcha_code: str) -> LoginSession:
        with self._lock:
            username = username.strip()
            if username in self._locked_accounts:
                raise MockDomainError(
                    423,
                    "account_locked",
                    f"账号 {username} 连续失败已达 {self.config.captcha_max_failures} 次，已锁定",
                )
            captcha = self._captchas.pop(captcha_id, None)
            if captcha is None or captcha.code.lower() != captcha_code.strip().lower():
                self._record_failure(username)
                raise MockDomainError(401, "captcha_invalid", "验证码不正确或已失效")
            if self.config.accounts.get(username) != password:
                self._record_failure(username)
                raise MockDomainError(401, "credentials_invalid", "账号或密码不正确")
            self._failures.pop(username, None)
            return self._open_login_session(username)

    def _record_failure(self, username: str) -> None:
        count = self._failures.get(username, 0) + 1
        self._failures[username] = count
        if count >= self.config.captcha_max_failures:
            self._locked_accounts.add(username)

    def is_locked(self, username: str) -> bool:
        with self._lock:
            return username.strip() in self._locked_accounts

    def _open_login_session(self, username: str) -> LoginSession:
        session = LoginSession(sid=_token("s_"), username=username, created_at=self._clock())
        self._login_sessions[session.sid] = session
        return session

    def login_session(self, sid: str | None) -> LoginSession:
        with self._lock:
            session = self._login_sessions.get(sid or "")
            if session is None:
                raise MockDomainError(401, "session_required", "请先登录账号站")
            return session

    def issue_qr_ticket(self) -> QrTicket:
        with self._lock:
            ticket = QrTicket(ticket=_token("qr_"), created_at=self._clock())
            self._qr_tickets[ticket.ticket] = ticket
            return ticket

    def _require_qr(self, ticket: str) -> QrTicket:
        record = self._qr_tickets.get(ticket)
        if record is None:
            raise MockDomainError(404, "qr_not_found", "二维码票据不存在")
        return record

    def _qr_expired(self, record: QrTicket) -> bool:
        return self._clock() - record.created_at > self.config.qr_ttl_seconds

    def qr_status(self, ticket: str) -> str:
        with self._lock:
            record = self._require_qr(ticket)
            if record.consumed_at is not None:
                return "consumed"
            if record.confirmed_at is not None:
                return "confirmed"
            if self._qr_expired(record):
                return "expired"
            return "pending"

    def confirm_qr(self, ticket: str, *, username: str = "") -> QrTicket:
        """模拟「手机端点了确认」。重复确认是幂等的，过期后不再接受。"""

        with self._lock:
            record = self._require_qr(ticket)
            if record.confirmed_at is None:
                if self._qr_expired(record):
                    raise MockDomainError(410, "qr_expired", "二维码已过期，请刷新")
                record.confirmed_at = self._clock()
                record.username = username.strip() or "qr-user"
            return record

    def open_qr_session(self, ticket: str) -> LoginSession:
        with self._lock:
            record = self._require_qr(ticket)
            if record.consumed_at is not None:
                raise MockDomainError(409, "qr_consumed", "这张二维码已经换过登录态了")
            if record.confirmed_at is None:
                if self._qr_expired(record):
                    raise MockDomainError(410, "qr_expired", "二维码已过期，请刷新")
                raise MockDomainError(409, "qr_pending", "手机上还没确认")
            record.consumed_at = self._clock()
            return self._open_login_session(record.username)

    def issue_handoff(self, *, sid: str, service_id: str) -> HandoffTicket:
        """跨站票据：账号站签发，办理站兑换。一次性、有 TTL。"""

        with self._lock:
            session = self.login_session(sid)
            service_by_id(service_id)
            ticket = HandoffTicket(
                ticket=_token("hand_"),
                service_id=service_id,
                username=session.username,
                created_at=self._clock(),
            )
            self._handoffs[ticket.ticket] = ticket
            return ticket

    def redeem_handoff(self, ticket: str) -> ApplySession:
        with self._lock:
            record = self._handoffs.get(ticket)
            if record is None:
                raise MockDomainError(404, "handoff_invalid", "跨站票据无效")
            if record.used_at is not None:
                raise MockDomainError(409, "handoff_used", "跨站票据已被使用")
            if self._clock() - record.created_at > self.config.handoff_ttl_seconds:
                raise MockDomainError(410, "handoff_expired", "跨站票据已过期")
            record.used_at = self._clock()
            session = ApplySession(
                sid=_token("a_"),
                username=record.username,
                service_id=record.service_id,
                created_at=self._clock(),
            )
            self._apply_sessions[session.sid] = session
            return session

    # ------------------------------------------------------------------ 办理站

    def apply_session(self, sid: str | None) -> ApplySession:
        with self._lock:
            session = self._apply_sessions.get(sid or "")
            if session is None:
                raise MockDomainError(401, "session_required", "请从账号站的事项列表进入办理站")
            return session

    def save_basic(self, sid: str, *, domicile_type: str, situations: list[str],
                   phone: str, address: str) -> list[dict[str, str]]:
        """保存 tab1。返回逐字段的错误；有任何错误就整体不落盘。"""

        with self._lock:
            session = self.apply_session(sid)
            errors: list[dict[str, str]] = []
            if domicile_type not in _DOMICILE_CODES:
                errors.append({"field": "domicile_type", "message": "请选择户籍类型"})
            picked = [item for item in dict.fromkeys(situations) if item in _SITUATION_CODES]
            if not picked:
                errors.append({"field": "situations", "message": "请至少选择一种办理情形"})
            phone = phone.strip()
            if not PHONE_PATTERN.fullmatch(phone):
                errors.append({"field": "phone", "message": "联系电话需为 11 位手机号"})
            address = address.strip()
            if not 5 <= len(address) <= 120:
                errors.append({"field": "address", "message": "联系地址需 5-120 个字"})
            if errors:
                return errors
            session.draft.domicile_type = domicile_type
            session.draft.situations = picked
            session.draft.phone = phone
            session.draft.address = address
            return []

    def add_attachment(self, sid: str, *, filename: str, content_type: str,
                       content: bytes) -> Attachment:
        with self._lock:
            session = self.apply_session(sid)
            draft = session.draft
            name = (filename or "").strip()
            if not name:
                raise MockDomainError(400, "attachment_nameless", "缺文件名")
            if not content:
                raise MockDomainError(400, "attachment_empty", "空文件不算材料")
            if content_type not in self.config.allowed_upload_types:
                raise MockDomainError(
                    415,
                    "attachment_type_rejected",
                    f"不接受的文件类型：{content_type or '未声明'}",
                    detail="allowed=" + ",".join(self.config.allowed_upload_types),
                )
            if len(content) > self.config.max_upload_bytes:
                raise MockDomainError(
                    413,
                    "attachment_too_large",
                    f"单个文件上限 {self.config.max_upload_bytes} 字节，实际 {len(content)}",
                )
            if len(draft.attachments) >= self.config.max_attachments:
                raise MockDomainError(
                    409,
                    "attachment_limit_reached",
                    f"最多 {self.config.max_attachments} 个附件",
                )
            attachment = Attachment(
                file_id=_token("f_"), filename=name, content_type=content_type, size=len(content)
            )
            draft.attachments.append(attachment)
            return attachment

    def delete_attachment(self, sid: str, file_id: str) -> None:
        """删除是幂等的：删一个不存在的东西不报错，重试不会失败。"""

        with self._lock:
            session = self.apply_session(sid)
            session.draft.attachments = [
                item for item in session.draft.attachments if item.file_id != file_id
            ]

    def submit_blockers(self, sid: str) -> list[str]:
        """返回人话版的阻塞项。空列表表示可以提交。

        页面上要展示「还差什么」，提交接口要据此拒绝，两处用同一份判断——否则
        页面说可以提交、接口返回 409，就成了靶子自己在误导执行器。
        """

        with self._lock:
            session = self.apply_session(sid)
            draft = session.draft
            blockers: list[str] = []
            if draft.domicile_type is None:
                blockers.append("基本信息还没填（户籍类型）")
            if not draft.situations:
                blockers.append("还没选办理情形")
            if not draft.phone:
                blockers.append("还没填联系电话")
            if not draft.address:
                blockers.append("还没填联系地址")
            count = len(draft.attachments)
            if count < self.config.min_attachments:
                blockers.append(
                    f"材料还差 {self.config.min_attachments - count} 个"
                    f"（至少 {self.config.min_attachments} 个）"
                )
            elif count > self.config.max_attachments:
                blockers.append(f"材料超过上限（最多 {self.config.max_attachments} 个）")
            return blockers

    def require_submittable(self, sid: str) -> ApplySession:
        with self._lock:
            session = self.apply_session(sid)
            blockers = self.submit_blockers(sid)
            if blockers:
                raise MockDomainError(
                    409,
                    "not_submittable",
                    "材料还没齐，不能提交",
                    detail="；".join(blockers),
                )
            return session

    def submit(self, sid: str) -> Receipt:
        """生成回执。**不查重**：同一个草稿提交两次就是两张回执。

        这是刻意的——「重复请求不该重复产生副作用」是产品要证明的事，模拟站不替它兜底。
        """

        with self._lock:
            session = self.require_submittable(sid)
            draft = session.draft
            self._receipt_seq += 1
            now = self._clock()
            no = f"MOCK-{time.strftime('%Y%m%d', time.localtime(now))}-{self._receipt_seq:04d}"
            receipt = Receipt(
                no=no,
                service_id=session.service_id,
                username=session.username,
                domicile_type=draft.domicile_type or "",
                situations=tuple(draft.situations),
                attachments=len(draft.attachments),
                created_at=now,
            )
            self._receipts[no] = receipt
            return receipt

    def receipt(self, no: str) -> Receipt:
        with self._lock:
            record = self._receipts.get(no)
            if record is None:
                raise MockDomainError(404, "receipt_not_found", f"没有这张回执：{no}")
            return record

    def receipts_for(self, sid: str) -> list[Receipt]:
        with self._lock:
            session = self.apply_session(sid)
            return [item for item in self._receipts.values() if item.username == session.username]
