"""失败现场落盘的脱敏回归：**不需要浏览器**。

`session` 只在浏览器里跑得起来，但"落盘前把敏感值抹掉"这件事必须能在任何环境
下被验证——它是 #79 里唯一一条"出事以后无法补救"的约束（截图/DOM 一旦带着身份
证号落地，就只能去追文件了）。
"""

from __future__ import annotations

from workpanda.harness.browser.artifacts import BrowserArtifacts
from workpanda.harness.redaction import MASK


def test_dom_is_redacted_before_it_reaches_the_disk(workdir) -> None:
    artifacts = BrowserArtifacts(root=workdir / "artifacts")
    dom = (
        '<html><body>'
        '<input name="phone" value="13800001234">'
        "<p>身份证 320501199001011234</p>"
        '<input name="password" value="demo1234">'
        '<input name="note" value="流水号 A1B2C3D4">'
        "</body></html>"
    )

    paths = artifacts.write(
        task_id="task-1",
        label="browser_click",
        dom_html=dom,
        screenshot=b"\x89PNG\r\n\x1a\n",
        # 模型填过的值由 session 记下来，落盘时再抹一道（这类值不一定长得像手机号）。
        secrets=["A1B2C3D4"],
    )

    written = paths[0].read_text(encoding="utf-8")
    assert "13800001234" not in written
    assert "320501199001011234" not in written
    assert "A1B2C3D4" not in written
    # 掩码是"替换"而不是"删掉整个文件"：留档的意义就是还能看出页面结构。
    assert MASK in written
    assert "<input name=" in written
    # 同一次写入的截图也落盘了，且与 DOM 分开。
    assert [path.suffix for path in paths] == [".html", ".png"]


def test_artifact_files_are_private_and_trimmed(workdir) -> None:
    artifacts = BrowserArtifacts(root=workdir / "artifacts", max_files=2)

    for index in range(5):
        artifacts.write(
            task_id="task-1",
            label=f"browser_wait-{index}",
            dom_html=f"<html><body>{index}</body></html>",
            screenshot=None,
        )

    files = sorted((workdir / "artifacts" / "task-1").iterdir())
    # 失败循环里每次调用都会留一份现场：不设上限就是给模型一条写满磁盘的路径。
    assert len(files) == 2
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"
    assert oct(files[0].parent.stat().st_mode & 0o777) == "0o700"


def test_empty_capture_writes_nothing(workdir) -> None:
    artifacts = BrowserArtifacts(root=workdir / "artifacts")

    assert artifacts.write(task_id="task-1", label="x", dom_html="", screenshot=None) == []
    assert not list((workdir / "artifacts").rglob("*"))


def test_oversized_dom_is_truncated(workdir) -> None:
    artifacts = BrowserArtifacts(root=workdir / "artifacts", max_dom_chars=100)

    paths = artifacts.write(
        task_id="task-1", label="big", dom_html="<html>" + "x" * 5000, screenshot=None
    )

    written = paths[0].read_text(encoding="utf-8")
    assert "已截断" in written
    assert len(written) < 200
