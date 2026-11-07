# WorkPanda 项目长期约定

## 交付流程（用户明确授权）

- **修改完成后直接合并 PR，无需等待人工审核。** 用户于 2026-11-06 明确指示，覆盖 AGENTS.md 中「PR 需由作者以外的人审查」的默认期望。
- 仍保留的硬性门禁（不因"免审核"而放松）：
  - 合并前 `pytest` 与 `ruff check .` 必须通过；
  - 合并前远端 CI 两项检查（Python 检查、文档与敏感信息基础检查）必须 success；
  - 高风险改动（身份核验、敏感数据、副作用语义、权限/流程变更）如实说明影响面，不带病合并。
- 仓库约定：代码改动走特性分支 + PR；squash 合并，标题保留 `(#N)`；`docs(memory)` 类日志提交按历史惯例可直接进 main。
- 分支清理：合并后可 `git -c http.proxy= -c https.proxy= push origin --delete <branch>`
  （直连偶尔需重试）；报 HTTP2 framing 错误时改用 GitHub API `DELETE /git/refs/heads/<branch>`，
  再 `git branch -rd` 清本地追踪引用。
- PR 操作（创建/合并/删分支）本机都走 GitHub API + 代理 `http://127.0.0.1:7897`；token 用 `git credential fill` 从 credential manager 取（`gh` 未装）。
- 建 PR 后必须等两项 CI（Python 检查、文档与敏感信息基础检查）为 success 再合并；用 `GET /commits/<sha>/check-runs` 核实。

## 环境事实（本机 Windows）

- 依赖：`.venv` + `pip install -e ".[dev]"`；`.env` 使用 `WORKPANDA_` 前缀，由 pydantic-settings 读取。
- `gh` 未安装；PR 用 git credential manager 里的凭据 + GitHub API 操作。
- 全量 `pytest` 约需 5 分钟，慢在 sqlite store 用例 teardown（15-17s/例）与 `test_http_fetch_*` 的 DNS 超时（约 5s/例），不是代码回归；长跑请后台执行并落盘日志。
- 本机 git 走代理，传输层偶发失败；必要时 `git -c http.version=HTTP/1.1`。
- `git push` 直连与代理两条链路都会抽风：直连常 `Failed to connect to github.com:443`，
  代理常 `curl 52 Empty reply` + schannel renegotiation。用直连/代理交替重试最稳
  （`git -c http.proxy= ... push` 与 `git -c http.proxy=http://127.0.0.1:7897 ... push`）；
  `fetch` 走代理通常即可。GitHub API 的 POST 也会被掐断，同样需要重试。
- 本机 Windows 原生 Python 看不到 Git Bash 的 `/tmp`；临时文件写到仓库内 `.pytest-tmp/`。

## 工程约定（跨任务复用）

- **任务状态写入只能用条件写**：凡决定「任务现在等谁」的写入（终态、暂停态、
  字段更新）都必须与它的事件走同一个 `TaskStore.commit(..., expected_status=...)`，
  期望值取**刚刚从库里读到**的状态，不能取调用方手里的对象（旧快照当期望值会让
  本该失败的条件写成功）；纯过程事件（`STEP_STARTED`/`STEP_COMPLETED`）才可即时写。
- **不留无条件写助手**：`FlowRunner._persist` 已删除。新增写路径前先问一句
  「这条写入落后了会不会留下一条无主事件」，会，就必须走条件写。

## 并发风险
- 工作区存在 `worker1`/`worker2` 两个每小时自动化，**与人工会话共用同一个 worktree**，会执行 `git commit -a`。因此：不要在共享 worktree 里留未提交文件；看到非自己所建的提交，先查 `git reflog`、作者与自动化列表再决定。
- 并发 worker 还会**切换分支、把别人的 PR squash 合入 main、整体还原文档文件**（本轮 `docs/HARNESS.md` 追加的第 15 节曾被还原）。对策：提交时只 `git add` 自己改过的具体路径，不用 `git add -A`；提交后立即复查关键内容；用 GitHub API 核对 PR 与 main 的真实状态，不要只信本地分支名。
