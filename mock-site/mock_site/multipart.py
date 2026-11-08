"""`multipart/form-data` 的最小解析器：只用标准库。

为什么不用 Starlette 的 `request.form()`：它解析 multipart 需要 `python-multipart`，
而本仓库没有装（`pip install -e .` 不含它）。为了一个靶子给产品加一条 dev 依赖，
等于把「被测对象」和「测试靶子」的依赖绑在一起——这正是 issue 要求避免的事。

为什么不用 `email` 解析器：它按 RFC 5322 处理头部，遇到浏览器直接发来的 UTF-8
中文文件名会编码成代理字符，渲染时随时炸成 `UnicodeEncodeError`。这里只认浏览器
实际会发的两种形状（`filename="..."` 与 `filename*=UTF-8''...`），行为可控。

解析器不做容错兜底：格式不对就 400，绝不猜。靶子如果「什么都能收」，就测不出
执行器是否发了正确的请求。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from mock_site.state import MockDomainError

BOUNDARY_PATTERN = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)


@dataclass(frozen=True)
class MultipartPart:
    name: str
    filename: str | None
    content_type: str
    content: bytes


def parse_multipart(content_type: str, body: bytes) -> list[MultipartPart]:
    boundary = BOUNDARY_PATTERN.search(content_type)
    if boundary is None:
        raise MockDomainError(400, "multipart_missing_boundary", "multipart 请求缺少 boundary")
    separator = b"--" + (boundary.group(1) or boundary.group(2)).encode("latin-1", "replace")
    parts: list[MultipartPart] = []
    for chunk in body.split(separator):
        if not chunk or chunk.startswith(b"--"):
            # 前导空段、以及结束分隔符 `--boundary--` 之后的尾巴，都不是 part。
            continue
        payload = _trim_crlf(chunk)
        head, separator_found, content = _split_headers(payload)
        if not separator_found:
            continue
        headers = _parse_headers(head)
        disposition = headers.get("content-disposition", "")
        filename = _param(disposition, "filename")
        parts.append(
            MultipartPart(
                name=_param(disposition, "name") or "",
                filename=filename,
                content_type=headers.get("content-type", "application/octet-stream"),
                content=content,
            )
        )
    return parts


def _trim_crlf(payload: bytes) -> bytes:
    for prefix in (b"\r\n", b"\n"):
        if payload.startswith(prefix):
            payload = payload[len(prefix):]
            break
    for suffix in (b"\r\n", b"\n"):
        if payload.endswith(suffix):
            return payload[: -len(suffix)]
    return payload


def _split_headers(payload: bytes) -> tuple[bytes, bool, bytes]:
    head, separator, content = payload.partition(b"\r\n\r\n")
    if separator:
        return head, True, content
    head, separator, content = payload.partition(b"\n\n")
    return head, bool(separator), content


def _parse_headers(head: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_line in head.replace(b"\r\n", b"\n").split(b"\n"):
        line = raw_line.decode("utf-8", "replace").strip()
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        headers[name.strip().lower()] = value.strip()
    return headers


def _param(header_value: str, key: str) -> str | None:
    """取 `key=value` 或 RFC 5987 的 `key*=UTF-8''value`。不跨参数误匹配。"""

    extended = re.search(rf"(?:^|[;\s]){key}\*=([^;]+)", header_value, re.IGNORECASE)
    if extended is not None:
        # RFC 5987 的形状是 charset'lang'值；charset 与 lang 可能是空串，但引号必在。
        extension = extended.group(1).strip().strip('"')
        pieces = extension.split("'", 2)
        if len(pieces) == 3:
            extension = pieces[2]
        return unquote(extension, encoding="utf-8", errors="replace")
    quoted = re.search(rf'(?:^|[;\s]){key}="([^"]*)"', header_value, re.IGNORECASE)
    if quoted is not None:
        return quoted.group(1)
    plain = re.search(rf"(?:^|[;\s]){key}=([^;]+)", header_value, re.IGNORECASE)
    if plain is not None:
        return plain.group(1).strip()
    return None
