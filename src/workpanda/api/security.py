"""会话解析与限流依赖。

会话令牌通过 `X-WorkPanda-Session` 传递。首次访问未带令牌时由服务端签发，
并在响应头回传；客户端后续请求需原样带回。
"""

from fastapi import Depends, Request, Response

from workpanda.api.dependencies import get_container
from workpanda.application.errors import AuthenticationRequired, RateLimited
from workpanda.bootstrap import Container
from workpanda.domain.identity import InvalidSession, Principal

SESSION_HEADER = "X-WorkPanda-Session"


def get_principal(
    request: Request,
    response: Response,
    container: Container = Depends(get_container),
) -> Principal:
    token = request.headers.get(SESSION_HEADER)
    if token:
        try:
            principal = container.signer.verify(token)
        except InvalidSession as exc:
            raise AuthenticationRequired("会话无效或已过期，请重新开始会话。") from exc
    else:
        token = container.signer.issue()
        principal = container.signer.verify(token)
        response.headers[SESSION_HEADER] = token

    if not container.limiter.allow(principal.subject):
        raise RateLimited(container.limiter.retry_after_seconds(principal.subject))
    return principal
