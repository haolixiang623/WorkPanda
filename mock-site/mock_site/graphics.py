"""验证码与二维码的图形：只有 SVG，不引图形库、不联网。

二维码是**占位图案**：它有定位角和一片确定性噪点，但不是可扫描的编码。真做 QR 编码
要引一个库或者手写几百行纠错码，而模拟站需要的是「页面上有一个能定位、能截图、会过期
的东西」；票据本身由 `data-testid="qr-ticket"` 暴露，执行器读文本而不是识图。
"""

from __future__ import annotations

import hashlib
import random
from base64 import b64encode
from html import escape
from urllib.parse import quote

CAPTCHA_WIDTH = 120
CAPTCHA_HEIGHT = 44


def as_data_uri(svg: str) -> str:
    """内联成 data URI：页面因此不需要任何外部请求，截图/定位也不受影响。"""

    return "data:image/svg+xml;base64," + b64encode(svg.encode("utf-8")).decode("ascii")


def render_captcha_svg(code: str) -> str:
    rng = random.Random(f"mock-captcha::{code}")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CAPTCHA_WIDTH}" '
        f'height="{CAPTCHA_HEIGHT}" viewBox="0 0 {CAPTCHA_WIDTH} {CAPTCHA_HEIGHT}" '
        'role="img" aria-label="图形验证码">',
        f'<rect width="{CAPTCHA_WIDTH}" height="{CAPTCHA_HEIGHT}" fill="#eef1f5"/>',
    ]
    for _ in range(5):
        x1 = rng.uniform(0, CAPTCHA_WIDTH)
        y1 = rng.uniform(0, CAPTCHA_HEIGHT)
        x2 = rng.uniform(0, CAPTCHA_WIDTH)
        y2 = rng.uniform(0, CAPTCHA_HEIGHT)
        parts.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            'stroke="#c3ccd8" stroke-width="1"/>'
        )
    step = CAPTCHA_WIDTH / (len(code) + 0.8)
    for index, char in enumerate(code):
        x = 12 + index * step + rng.uniform(-2.0, 2.0)
        y = CAPTCHA_HEIGHT * 0.7 + rng.uniform(-3.0, 3.0)
        angle = rng.uniform(-14.0, 14.0)
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="Menlo,Consolas,monospace" '
            f'font-size="26" fill="#2b3038" transform="rotate({angle:.1f} {x:.1f} {y:.1f})">'
            f"{escape(char)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def render_qr_svg(payload: str, *, modules: int = 21, scale: int = 5) -> str:
    """画一个确定性矩阵。同一个 payload 永远得到同一张图，方便截图比对。"""

    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    rng = random.Random(digest)
    quiet = 2
    size = (modules + quiet * 2) * scale
    cells: list[str] = []
    for row in range(modules):
        for column in range(modules):
            filled = _in_finder(row, column, modules) or rng.random() < 0.45
            if filled:
                cells.append(
                    f'<rect x="{(column + quiet) * scale}" y="{(row + quiet) * scale}" '
                    f'width="{scale}" height="{scale}" fill="#1f2328"/>'
                )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="扫码登录占位图案">'
        f'<rect width="{size}" height="{size}" fill="#ffffff"/>'
        + "".join(cells)
        + "</svg>"
    )


def _in_finder(row: int, column: int, modules: int) -> bool:
    """三个定位角：7x7 实心框 + 3x3 实心块。只是让它「看起来像二维码」。"""

    corners = ((0, 0), (0, modules - 7), (modules - 7, 0))
    for top, left in corners:
        if not (top <= row < top + 7 and left <= column < left + 7):
            continue
        local_row = row - top
        local_column = column - left
        on_border = local_row in (0, 6) or local_column in (0, 6)
        in_core = 2 <= local_row <= 4 and 2 <= local_column <= 4
        return on_border or in_core
    return False


def qr_payload(origin: str, ticket: str) -> str:
    """票据的可读形式：执行器可以直接拿它去调「手机确认」接口。"""

    return f"{origin}/login/qr/{quote(ticket)}"
