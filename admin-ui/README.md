# 管理侧 SOP 配置台前端

独立的管理侧页面，与群众侧对话入口分开构建、分开部署。只调用
`/api/admin/flows/*`，不引用群众侧任何资源，也不持有任何凭据。

接口、权限矩阵、影响面与已知限制见 [../docs/ADMIN-CONSOLE.md](../docs/ADMIN-CONSOLE.md)。

## 文件

| 路径 | 用途 |
|---|---|
| `index.html` | 页面骨架与样式 |
| `src/main.ts` | TypeScript 源码 |
| `tsconfig.json` | 编译配置（ES2020 / DOM / strict / noEmitOnError） |
| `dist/main.js` | 已提交的编译产物，部署只需要它和 `index.html` |

构建（产物已随仓库提交，仅在改源码后需要重跑）：

```bash
npx -p typescript@5.6.3 tsc --project admin-ui/tsconfig.json
```

编译必须零错误才产出文件；不要把未通过 strict 检查的源码手工改写成 JS 交付。

## 部署

静态托管 `index.html` 与 `dist/` 即可，无运行时依赖、无打包器、无 npm 依赖。
页面默认请求同源服务端，也可以在「服务端地址」里填写其它来源；跨域时服务端需把
该来源加入 `WORKPANDA_CORS_ORIGINS` 白名单。

服务端默认只监听回环且不启用 TLS；对外暴露的条件见
[../docs/OPS-ACCESS-CONTROL.md](../docs/OPS-ACCESS-CONTROL.md) 第 4 节。
管理侧独立域名 / 端口 / 证书仍未定稿（见 Issue #53）。

## 凭据

服务端通过 `WORKPANDA_ADMIN_TOKENS=主体:角色:密钥,...` 发放凭据，`viewer` 只读、
`admin` 可写。页面里的「管理侧凭据」由运维运行时输入，只保存在模块级内存变量中，
随 `X-WorkPanda-Admin-Token` 请求头发送：

- 不写入源码或构建产物；
- 不使用 localStorage / sessionStorage / cookie 持久化；
- 不回显到页面文本、日志或响应体；
- 与群众会话令牌、运维只读凭据互不通用。

回归用例 `tests/test_admin_flows_api.py::test_admin_ui_assets_do_not_embed_credentials`
会扫描本目录的交付文件，禁止出现凭据或持久化存储。

## 操作顺序

1. 填服务端地址与 admin 凭据，点「读取版本列表」。
2. 「新建流程草稿」：填 flow_id、version、标题，逐行配置步骤（类型、必填字段、
   prompt、instruction、tool、confirm_message、是否需确认），保存草稿。
   版本不可变，改流程必须新建版本号；status 由服务端固定为草稿。
3. 在「回放用例与测试」里选版本、加用例、点「运行回放」，看逐 step 结果与总判定。
   字段校验失败会标红对应输入框并在消息区列出 `loc`。
4. 「预演发布（不移动指针）」确认门禁结论；再点「发布」。
   被门禁拦下时消息区给出 409 与完整报告，界面不会显示成已发布。
5. 需要退回时「回滚到此版本」；要整体下线用「停用流程」。历史版本都保留。

## 不做的事

多级审批流、多人协作与评论、自由文本 DSL / YAML 编辑器、群众侧界面改造。
意图样例的运行时路由由 Issue #52 承接，多实例发布同步由 Issue #53 承接。
