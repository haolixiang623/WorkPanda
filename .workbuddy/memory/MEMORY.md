# WorkPanda 项目长期约定

## 交付流程（用户明确授权）

- **修改完成后直接合并 PR，无需等待人工审核。** 用户于 2026-11-06 明确指示，覆盖 AGENTS.md 中「PR 需由作者以外的人审查」的默认期望。
- 仍保留的硬性门禁（不因"免审核"而放松）：
  - 合并前 `pytest` 与 `ruff check .` 必须通过；
  - 合并前远端 CI 两项检查（Python 检查、文档与敏感信息基础检查）必须 success；
  - 高风险改动（身份核验、敏感数据、副作用语义、权限/流程变更）如实说明影响面，不带病合并。
- 仓库约定：代码改动走特性分支 + PR；squash 合并，标题保留 `(#N)`；`docs(memory)` 类日志提交按历史惯例可直接进 main。
- 分支清理：合并后删远端分支。`git push --delete` 在本机会报 HTTP2 framing 错误，改用 GitHub API `DELETE /git/refs/heads/<branch>`，再 `git branch -rd` 清本地追踪引用。

## 环境事实（本机 Windows）

- 依赖：`.venv` + `pip install -e ".[dev]"`；`.env` 使用 `WORKPANDA_` 前缀，由 pydantic-settings 读取。
- `gh` 未安装；PR 用 git credential manager 里的凭据 + GitHub API 操作。
- 全量 `pytest` 约需 5 分钟，慢在 sqlite store 用例 teardown（15-17s/例）与 `test_http_fetch_*` 的 DNS 超时（约 5s/例），不是代码回归；长跑请后台执行并落盘日志。
- 本机 git 走代理，传输层偶发失败；必要时 `git -c http.version=HTTP/1.1`。

## 并发风险

- 工作区存在 `worker1`/`worker2` 两个每小时自动化，**与人工会话共用同一个 worktree**，会执行 `git commit -a`。因此：不要在共享 worktree 里留未提交文件；看到非自己所建的提交，先查 `git reflog`、作者与自动化列表再决定。
