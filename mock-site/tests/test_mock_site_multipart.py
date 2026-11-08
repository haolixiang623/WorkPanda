"""multipart 解析器：只认浏览器实际会发的形状，不猜。

这里刻意用原始字节构造请求体，而不是借 httpx 的编码器——解析器要能在「别人怎么编码」
上站得住，用同一个库编码再解码等于自己证明自己。
"""

from __future__ import annotations

import pytest
from mock_site.multipart import parse_multipart
from mock_site.state import MockDomainError

BOUNDARY = "----MockBoundary42"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def build_body(parts: list[tuple[str, str | None, str | None, bytes]]) -> bytes:
    chunks: list[bytes] = []
    for name, filename, content_type, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(f"--{BOUNDARY}\r\n".encode())
        chunks.append(f"Content-Disposition: {disposition}\r\n".encode())
        if content_type is not None:
            chunks.append(f"Content-Type: {content_type}\r\n".encode())
        chunks.append(b"\r\n")
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


def test_reads_two_files_including_a_chinese_filename() -> None:
    body = build_body(
        [
            ("files", "户主页.pdf", "application/pdf", b"%PDF-1.4 first"),
            ("files", "身份证.png", "image/png", b"\x89PNG\r\n\x1a\nbinary"),
        ]
    )
    parts = parse_multipart(CONTENT_TYPE, body)
    assert [part.filename for part in parts] == ["户主页.pdf", "身份证.png"]
    assert [part.content_type for part in parts] == ["application/pdf", "image/png"]
    assert parts[0].content == b"%PDF-1.4 first"
    # 二进制内容里的 \r\n 不能被当成边界或行尾吃掉。
    assert parts[1].content == b"\x89PNG\r\n\x1a\nbinary"


def test_reads_rfc5987_filename_star() -> None:
    body = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="files"; '
        "filename*=UTF-8''%E6%9D%90%E6%96%99.pdf\r\n"
        "Content-Type: application/pdf\r\n\r\n"
        "payload\r\n"
        f"--{BOUNDARY}--\r\n"
    ).encode()
    parts = parse_multipart(CONTENT_TYPE, body)
    assert parts[0].filename == "材料.pdf"


def test_plain_field_has_no_filename() -> None:
    body = build_body([("note", None, None, b"hello")])
    parts = parse_multipart(CONTENT_TYPE, body)
    assert parts[0].name == "note"
    assert parts[0].filename is None
    assert parts[0].content == b"hello"


def test_name_is_not_taken_from_the_filename_parameter() -> None:
    """`filename="x.pdf"` 里也含 `name=`，正则必须带边界，否则会取到错的值。"""

    body = build_body([("files", "x.pdf", "application/pdf", b"data")])
    parts = parse_multipart(CONTENT_TYPE, body)
    assert parts[0].name == "files"


def test_missing_boundary_is_a_400() -> None:
    with pytest.raises(MockDomainError) as caught:
        parse_multipart("multipart/form-data", b"whatever")
    assert caught.value.status == 400
    assert caught.value.code == "multipart_missing_boundary"


def test_empty_body_yields_no_parts() -> None:
    assert parse_multipart(CONTENT_TYPE, b"") == []


def test_quoted_boundary_is_accepted() -> None:
    body = build_body([("files", "a.pdf", "application/pdf", b"x")])
    parts = parse_multipart(f'multipart/form-data; boundary="{BOUNDARY}"', body)
    assert parts[0].filename == "a.pdf"


def test_part_without_content_type_defaults_to_octet_stream() -> None:
    body = build_body([("files", "a.pdf", None, b"x")])
    parts = parse_multipart(CONTENT_TYPE, body)
    assert parts[0].content_type == "application/octet-stream"
