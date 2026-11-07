# 试点持久部署

把本机试点从「`/tmp` 里手工启的两个后台进程」换成 launchd 托管的常驻服务：
机器重启后自动拉起，数据与虚拟环境都在持久目录，凭据只存登录钥匙串。

## 边界

- 只监听 `127.0.0.1`；本工具不接受 `0.0.0.0`（传了直接报错），也不配置 TLS。
  对外暴露属于安全边界变更，见多用户权限隔离那条 Issue。
- 端口固定：Octop `8090`、WorkPanda `8010`（8088 被 Tomcat、8000 被别的
  FastAPI 服务占用）。
- 凭据只进钥匙串（service `workpanda-deploy`）；仓库、plist、`api.env`
  里都不会出现明文。`api.env` 只允许白名单内的非敏感键，写敏感键会被拒绝。

## 目录布局

状态根目录默认 `~/Library/Application Support/WorkPanda`（`--root` 可改）：

| 路径 | 作用 |
|---|---|
| `api-venv/` | WorkPanda 自己的 venv（`pip install <仓库>`，与工作树解耦） |
| `octop-venv/` | Octop 平台 venv（默认 `octop==1.0.2`） |
| `octop-home/` | Octop 的 `HOME`，`.octop`（数据库、插件、模型配置）在其下 |
| `data/workpanda.db` | 任务库（SQLite），重启不丢 |
| `logs/api.log`、`logs/octop.log` | launchd 收集的标准输出与错误 |
| `bin/run-api.sh`、`bin/run-octop.sh` | 启动包装脚本（0700，运行时从钥匙串补凭据） |
| `api.env` | 非敏感配置（0600），由包装脚本 source |

launchd 单元写在 `~/Library/LaunchAgents/com.workpanda.{api,octop}.plist`，
`RunAtLoad` + `KeepAlive`，异常退出由 launchd 拉起。

## 首次安装（含从 /tmp 迁移）

```bash
# 1. 凭据挑一次写进钥匙串（值从标准输入读，不进 shell 历史）
printf '%s' "$WORKPANDA_OPS_TOKEN" | python3 deploy/workpanda_deploy.py secret set WORKPANDA_OPS_TOKEN
printf '%s' "$MODEL_API_KEY"       | python3 deploy/workpanda_deploy.py secret set WORKPANDA_MODEL_API_KEY
printf '%s' "$SESSION_SECRET"      | python3 deploy/workpanda_deploy.py secret set WORKPANDA_SESSION_SECRET

# 2. 迁移旧数据并安装（--no-start 只落盘）
python3 deploy/workpanda_deploy.py install \
  --repo "$PWD" \
  --from-octop-home /tmp/wp-octop-home \
  --from-sqlite "$PWD/workpanda.db" \
  --set-env WORKPANDA_HARNESS_ENABLED=true \
  --set-env WORKPANDA_MODEL_BASE_URL=https://example.invalid/v1 \
  --set-env WORKPANDA_MODEL_NAME=example-model \
  --no-start

# 3. 停掉旧的手工进程后引导 launchd（两个单元按依赖顺序起来）
launchctl bootout "gui/$UID/com.workpanda.api" 2>/dev/null || true
python3 deploy/workpanda_deploy.py install --repo "$PWD"   # 省略迁移参数即幂等复跑
```

`install` 会重建 venv、写 `api.env` 与包装脚本、重写 plist、`bootout` 后
`bootstrap`，最后打印探针结果；探针未就绪返回非 0。

## 日常操作

```bash
python3 deploy/workpanda_deploy.py status        # launchd 状态 + 端口探针 + 钥匙串清单
python3 deploy/workpanda_deploy.py secret list   # 只看「已配置 / 缺失」，不回显值
python3 deploy/workpanda_deploy.py secret set WORKPANDA_OPS_TOKEN   # 轮换，值走 stdin
python3 deploy/workpanda_deploy.py plugin        # 按目录重装插件 + 离线 reload + 重启 Octop
python3 deploy/workpanda_deploy.py uninstall     # 停服务并移除 plist（--purge-data 才删数据）
```

日志：`tail -f "~/Library/Application Support/WorkPanda/logs/octop.log"`。

## 验收口径

`status` 里以下几项能对上，就算持久化到位：

- `agents` 两项都是 `running`；
- `api_healthz` 与 `octop_health` 都是 `200`；
- `api_ops` 是 `200`（说明钥匙串里的 `WORKPANDA_OPS_TOKEN` 真的注入到了进程）；
- `octop_plugins_anonymous` 是 `401`（说明没有为了排障把控制台开放出去）。

插件是否 `loaded`、4 个工具是否 `enabled` 属于需要登录的界面状态，自动化只做
两件可脚本化的事：`plugin` 子命令按目录重装 + 离线 `reload` 后重启进程，以及
在 Octop 的 venv 里真实调用 4 个工具取数。控制台插件页的「已加载」标签仍需
登录后人工确认一次。

## 回滚

```bash
python3 deploy/workpanda_deploy.py uninstall          # 停服务、删 plist，保留数据
# 需要临时手工排障时，可直接前台运行 bin/run-api.sh 或 bin/run-octop.sh（先 uninstall 释放端口）
```

`uninstall --purge-data` 会连同 `data/`、`octop-home/` 一起删掉，不可恢复。
