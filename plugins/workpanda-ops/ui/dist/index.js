/* WorkPanda 运维只读插件 UI。
 *
 * 约束：自包含 ESM，不能有 import / 相对引用（Octop 用 blob URL 动态加载）；
 * React 由 Dashboard 注入到 window.__OCTOP_REACT__，这里只取 createElement。
 * 纯展示：不发写请求、不调用 host.request 的写方法、不修改 Task Store。
 */

const ReactGlobal = globalThis.__OCTOP_REACT__ || {};
const h = typeof ReactGlobal.createElement === "function" ? ReactGlobal.createElement : null;

const BORDER = "rgba(128,128,128,0.28)";
const MUTED = "rgba(128,128,128,0.95)";

const STYLE = {
  card: {
    border: `1px solid ${BORDER}`,
    borderRadius: 10,
    padding: 14,
    margin: "8px 0",
    fontFamily: "inherit",
    color: "inherit",
    background: "transparent",
    fontSize: 13,
    lineHeight: 1.5,
  },
  header: { display: "flex", alignItems: "center", gap: 8, marginBottom: 10 },
  title: { fontWeight: 600, fontSize: 14 },
  badge: {
    border: `1px solid ${BORDER}`,
    borderRadius: 999,
    padding: "1px 8px",
    fontSize: 11,
    color: MUTED,
  },
  error: {
    border: "1px solid rgba(220,60,60,0.55)",
    borderRadius: 8,
    padding: "8px 10px",
    marginBottom: 10,
    color: "rgb(220,60,60)",
  },
  note: { color: MUTED, fontSize: 12, marginTop: 10 },
  more: { color: MUTED, fontSize: 12, marginTop: 8, wordBreak: "break-all" },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(132px, 1fr))",
    gap: 8,
  },
  metric: { border: `1px solid ${BORDER}`, borderRadius: 8, padding: "8px 10px" },
  metricLabel: { color: MUTED, fontSize: 11, marginBottom: 2 },
  metricValue: { fontSize: 17, fontWeight: 600 },
  tableWrap: { overflowX: "auto" },
  table: { borderCollapse: "collapse", width: "100%", fontSize: 12 },
  th: {
    textAlign: "left",
    borderBottom: `1px solid ${BORDER}`,
    padding: "5px 8px",
    color: MUTED,
    whiteSpace: "nowrap",
  },
  td: { borderBottom: `1px solid ${BORDER}`, padding: "5px 8px", whiteSpace: "nowrap" },
  empty: { color: MUTED, padding: "10px 0" },
};

const LABELS = {
  total: "留痕总数", succeeded: "成功", failed: "失败", unconfirmed: "未确认",
  unconfirmed_rate: "未确认占比", tool_calls: "工具调用", blocked_tool_calls: "被拦调用",
  failed_tool_calls: "失败调用", timed_out_tool_calls: "超时调用",
  skipped_tool_calls: "跳过调用", avg_model_calls: "平均模型轮次",
  avg_duration_ms: "平均耗时(ms)", tokens_total: "token 合计",
  available: "可用", live: "活跃线程", stuck: "卡住线程", max_stuck: "卡住上限",
  task_id: "任务 ID", flow_id: "流程", flow_version: "版本", status: "状态",
  current_step: "当前步骤", failure_code: "失败码", created_at: "创建时间",
  updated_at: "更新时间", trace_id: "留痕 ID", step_id: "步骤", outcome: "结果",
  stop_reason: "停止原因", error_code: "错误码", risk: "风险", model_calls: "模型轮次",
  successful_tool_calls: "成功调用", duration_ms: "耗时(ms)", entry_id: "归档 ID",
  kind: "类型", effect: "副作用", requires_manual_check: "需人工核对",
};

function label(key) {
  return LABELS[key] || key;
}

function short(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "boolean") return value ? "是" : "否";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.length > 48 ? `${text.slice(0, 45)}…` : text;
}

function Header() {
  return h("div", { style: STYLE.header },
    h("span", { style: STYLE.title }, "WorkPanda 运维视图"),
    h("span", { style: STYLE.badge }, "只读"),
  );
}

function Metrics(props) {
  const groups = [props.metrics, props.tool_runtime];
  const entries = [];
  for (const group of groups) {
    if (!group || typeof group !== "object") continue;
    for (const key of Object.keys(group)) {
      const value = group[key];
      if (typeof value === "object" && value !== null) continue;
      entries.push([key, value]);
    }
  }
  if (!entries.length) return h("div", { style: STYLE.empty }, "暂无指标数据");
  return h("div", { style: STYLE.grid },
    entries.map(([key, value]) =>
      h("div", { key, style: STYLE.metric },
        h("div", { style: STYLE.metricLabel }, label(key)),
        h("div", { style: STYLE.metricValue }, short(value)),
      ),
    ),
  );
}

function Table(props) {
  const items = Array.isArray(props.items) ? props.items : [];
  if (!items.length) return h("div", { style: STYLE.empty }, "暂无记录");
  const keys = Object.keys(items[0] || {}).slice(0, 8);
  if (!keys.length) return h("div", { style: STYLE.empty }, "记录中没有可展示字段");
  return h("div", { style: STYLE.tableWrap },
    h("table", { style: STYLE.table },
      h("thead", null,
        h("tr", null, keys.map((key) => h("th", { key, style: STYLE.th }, label(key)))),
      ),
      h("tbody", null,
        items.map((item, index) =>
          h("tr", { key: index },
            keys.map((key) => h("td", { key, style: STYLE.td }, short(item[key]))),
          ),
        ),
      ),
    ),
  );
}

function WorkpandaOps(props) {
  if (!h) return null;
  const root = props || {};
  const raw = root.data;
  const data = raw && typeof raw === "object" ? raw : {};
  const view = data.view || "overview";
  const children = [h(Header, { key: "header" })];

  if (data.error) {
    children.push(h("div", { key: "error", style: STYLE.error }, short(data.error)));
  }
  if (root.status === "running") {
    children.push(h("div", { key: "running", style: STYLE.note }, "正在读取…"));
  } else if (view === "overview") {
    children.push(h(Metrics, {
      key: "metrics",
      metrics: data.metrics,
      tool_runtime: data.tool_runtime,
    }));
  } else {
    children.push(h(Table, { key: "table", items: data.items }));
    const next = typeof data.next_cursor === "string" ? data.next_cursor.trim() : "";
    if (next) {
      // 截断只影响展示，不影响回传：游标必须原样送回服务端。
      children.push(h("div", { key: "more", style: STYLE.more },
        `还有下一页（cursor: ${short(next)}）。把 next_cursor 原样作为 cursor 参数再调一次即可。`,
      ));
    }
  }

  children.push(h("div", { key: "note", style: STYLE.note },
    "数据来源：WorkPanda /api/ops/* 只读接口。此视图不提供改状态、重试或取消操作；",
    "需要人工处置时请走既定转人工渠道。",
  ));

  return h("div", { style: STYLE.card }, children);
}

export function setup(host) {
  if (!h) {
    // 宿主未注入 React 时仍注册渲染器，避免工具调用因缺少 UI 而失败。
    console.error("[workpanda-ops] window.__OCTOP_REACT__.createElement 不可用，UI 降级为空");
  }
  host.registerRenderer({
    id: "workpanda_ops",
    tools: [
      "workpanda_ops_overview",
      "workpanda_ops_tasks",
      "workpanda_ops_traces",
      "workpanda_ops_audit",
    ],
    component: h ? WorkpandaOps : function EmptyComponent() { return null; },
  });
}

export default { setup };
