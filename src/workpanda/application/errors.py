"""应用层错误，由 API 层统一映射为 HTTP 状态码。"""


class TaskConflict(Exception):
    """任务当前状态不允许该操作。"""


class RequestRejected(ValueError):
    """请求内容不符合当前任务或流程约束。"""


class IntentUnresolved(Exception):
    """无法把输入解析为已发布流程，需要澄清或转人工。"""


class AuthenticationRequired(Exception):
    """会话令牌缺失或无效。"""


class RateLimited(Exception):
    """请求过于频繁。"""

    def __init__(self, retry_after_seconds: int = 1) -> None:
        super().__init__("too many requests")
        self.retry_after_seconds = retry_after_seconds


class CancelNotDelivered(Exception):
    """取消信号没能送达执行侧：任务状态不得翻转，调用方应重试。"""
