"""失败现场留档：把 DOM 与截图写到磁盘，**先脱敏再落盘**。

为什么要留档：转人工的人需要看到"那一刻页面长什么样"，否则"定位失败"只剩一句
无从核查的话。但留档本身就是在磁盘上多写一份群众数据，所以三条约束缺一不可：

1. **默认不留**：`artifact_dir` 为空就什么都不写；
2. **落盘前脱敏**：DOM 走 `redact_text`（身份证/手机号/银行卡/令牌）并用本次任务
   填过的值再抹一遍（页面的 `value="..."` 会把填进去的手机号原样写进 HTML）；
3. **截图只盖住表单值**：截图前注入一段只读样式把输入框内容变成圆点。这是**像素级
   脱敏做不到**的诚实说法——页面上别处渲染的同一个人信息（例如摘要里回显的姓名）
   不会被盖住。所以：截图是给人看的诊断材料，不是可以随便外发的证据，目录权限
   必须收紧（这里按 0600 / 0700 建）。

每个任务一个子目录，超过 `max_files` 时按修改时间丢最旧的：失败循环里每次调用都会
留一份现场，不设上限就等于给了模型一个把磁盘写满的路径。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from workpanda.harness.redaction import mask_values, redact_text

# 文件名里只留安全字符：`label` 来自工具名，但这里不假设上游一定干净。
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")

# 单份 DOM 的上限。真实政务页面常见几百 KB，超过这个数多半是页面已经跑飞了。
MAX_DOM_CHARS = 500_000


@dataclass(frozen=True)
class BrowserArtifacts:
    """失败现场落盘器。只写、不改、不删别人的文件。"""

    root: Path
    max_files: int = 20
    max_dom_chars: int = MAX_DOM_CHARS

    def write(
        self,
        *,
        task_id: str,
        label: str,
        dom_html: str,
        screenshot: bytes | None,
        secrets: Sequence[str] = (),
    ) -> list[Path]:
        """写入一份现场，返回落盘路径。任何 I/O 失败都不抛给调用方。"""

        # 什么都没抓到就别建目录：一次失败的导航也会走到这里，空文件只会让人
        # 以为"留档了却什么都没有"。
        if not dom_html and not screenshot:
            return []

        try:
            directory = self._directory(task_id)
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        except OSError:
            return []

        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        suffix = _SAFE.sub("-", label).strip("-")[:24] or "failure"
        written: list[Path] = []

        dom_path = directory / f"{stamp}-{suffix}.html"
        if self._write(dom_path, self._redact(dom_html, secrets)):
            written.append(dom_path)

        if screenshot:
            png_path = directory / f"{stamp}-{suffix}.png"
            if self._write(png_path, screenshot):
                written.append(png_path)

        self._trim(directory)
        return written

    def _directory(self, task_id: str) -> Path:
        return self.root / (_SAFE.sub("-", str(task_id)).strip("-") or "unknown")

    def _redact(self, dom_html: str, secrets: Sequence[str]) -> str:
        text = mask_values(redact_text(dom_html), [str(item) for item in secrets])
        if len(text) > self.max_dom_chars:
            text = text[: self.max_dom_chars] + "\n<!-- 已截断 -->"
        return text

    def _write(self, path: Path, payload: str | bytes) -> bool:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        try:
            # `x` 而非 `w`：同名文件已存在时不覆盖别人留下的现场。
            with path.open("xb") as handle:
                handle.write(data)
            path.chmod(0o600)
        except OSError:
            return False
        return True

    def _trim(self, directory: Path) -> None:
        if self.max_files <= 0:
            return
        try:
            files = sorted(
                (item for item in directory.iterdir() if item.is_file()),
                key=lambda item: item.stat().st_mtime,
            )
        except OSError:
            return
        for stale in files[: max(0, len(files) - self.max_files)]:
            try:
                stale.unlink()
            except OSError:
                continue
