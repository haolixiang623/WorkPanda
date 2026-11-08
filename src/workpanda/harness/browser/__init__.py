"""浏览器执行器（P2）：Playwright 驱动，只开到「进入填写页」。

三个模块，边界写死：

- `policy`：纯函数策略层，能去哪（域名 + 端口）、能点什么（副作用闸门）、
  失败语义。**不依赖 Playwright**，因此每条红线都能在没有浏览器的环境里回归。
- `session`：一任务一 browser + 一 context，把 Playwright 的线程绑定关在一个
  单线程执行器里；顺带做失败现场留档。
- `tools`：七个工具，只负责"怎么操作"，"能不能操作"一律问策略层。

Playwright 是可选依赖（`pip install '.[browser]'` + `playwright install chromium`），
所以这里的导入全部延迟到真正要用的时候——没装可选依赖的部署照常起服务。
"""

from __future__ import annotations

from workpanda.harness.browser.policy import BrowserPolicy, policy_from_hosts
from workpanda.harness.browser.session import BrowserSession, BrowserSessions
from workpanda.harness.browser.tools import browser_tools

__all__ = [
    "BrowserPolicy",
    "BrowserSession",
    "BrowserSessions",
    "browser_tools",
    "policy_from_hosts",
]
