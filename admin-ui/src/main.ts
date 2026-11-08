/**
 * 管理侧 SOP 配置台（Issue #51）的交互层。
 *
 * 与群众侧分开构建、分开部署：本页只调 /api/admin/flows/*，不引用群众侧任何
 * 资源。管理侧凭据由运维在页面里输入，只保存在模块级变量中随请求头发送——
 * 不写 DOM 之外的存储、不进构建产物、不出现在群众侧 bundle 里。
 *
 * 界面刻意不自己推断"发布成功"：一切以服务端返回的 published 字段与状态码为准。
 * 预演返回 published=false，门禁拦下返回 409 + 报告，两者都不会显示成已发布。
 *
 * Issue #62：意图样例随草稿一起提交。保存前的本地预检只负责「别把明显不够材料的
 * 草稿发出去」，发布能否通过仍由服务端门禁裁决，本地数字与服务端漂移时以 409 为准。
 */

import {
  checkSamples,
  sampleGateDecision,
  type SampleCategory,
  type SampleInput,
  SAMPLE_CATEGORIES,
} from "./sample_check.js";

type StepKind = "collect" | "execute" | "handoff";

interface StepInput {
  step_id: string;
  kind: StepKind;
  title: string;
  required_fields: string[];
  prompt?: string;
  instruction?: string;
  tool?: string;
  confirm_message?: string;
  requires_confirmation: boolean;
}

interface FlowDraft {
  flow_id: string;
  version: string;
  title: string;
  steps: StepInput[];
  intent_samples: SampleInput[];
}

interface FlowSummary {
  flow_id: string;
  version: string;
  title: string;
  published: boolean;
  case_count: number;
  /** 指针行上的署名人；只有 `published=true` 的那一行有值。 */
  published_by: string | null;
  /** 停用人；挂在停用前最后指向的那一版上。 */
  disabled_by: string | null;
  /** 最后一次发布 / 回滚的时间；停用不改它，所以停用时间不在这里。 */
  updated_at: string | null;
}

interface ReplayCaseView {
  case_id: string;
  slots: Record<string, string>;
  expect: Record<string, unknown>;
}

interface VersionDetail {
  flow: FlowDraft;
  published: boolean;
  cases: ReplayCaseView[];
}

interface StepResultView {
  step_id: string;
  outcome: string;
}

interface CaseResultView {
  case_id: string;
  verdict: string;
  steps: StepResultView[];
  findings: Array<{ code: string; detail: string }>;
}

interface ReplayReport {
  flow_id: string;
  flow_version: string;
  verdict: string;
  results: CaseResultView[];
}

interface PublishOutcome {
  flow_id: string;
  version: string;
  published: boolean;
  replay_ran: boolean;
  message: string;
  /** 预演（apply=false）没有移动指针，因此为 null。 */
  published_by: string | null;
}

interface RollbackOutcome {
  flow_id: string;
  version: string;
  rolled_back: boolean;
  published_by: string;
}

interface DisableOutcome {
  flow_id: string;
  disabled: boolean;
  disabled_by: string;
}

interface FieldProblem {
  loc: Array<string | number>;
  message: string;
}

interface ValidationIssue {
  loc: Array<string | number>;
  msg: string;
}

interface IntentCoverage {
  category?: string;
  have?: number;
  need?: number;
  short?: boolean;
  label?: string;
}

interface IntentFinding {
  code?: string;
  detail?: string;
  blocking?: boolean;
}

/** 识别样例门禁的 409 报告：与回放报告结构不同，不能塞进 renderReport()。 */
interface IntentEvalReportPayload {
  status?: string;
  coverage?: IntentCoverage[];
  findings?: IntentFinding[];
}

interface ErrorPayload {
  code?: string;
  detail?: string | ValidationIssue[];
  fields?: FieldProblem[];
  report?: ReplayReport | IntentEvalReportPayload;
}

class ApiFailure extends Error {
  constructor(readonly status: number, readonly payload: ErrorPayload) {
    super(`HTTP ${status}`);
  }
}

function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) {
    throw new Error(`页面缺少元素 #${id}`);
  }
  return found as T;
}

function value(id: string): string {
  return el<HTMLInputElement | HTMLTextAreaElement>(id).value.trim();
}

function escapeHtml(raw: string): string {
  return raw.replace(/[&<>"']/g, (char) => {
    const table: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return table[char] ?? char;
  });
}

function showMessage(kind: "ok" | "err", html: string): void {
  const box = el<HTMLDivElement>("message");
  box.className = kind;
  box.innerHTML = html;
}

function clearInvalid(): void {
  document.querySelectorAll(".invalid").forEach((node) => node.classList.remove("invalid"));
}

/** 把服务端给的 loc 映射到表单输入框。FastAPI 的 422 会带一层 "body" 前缀。 */
function markInvalid(fields: FieldProblem[]): void {
  clearInvalid();
  for (const field of fields) {
    const key = field.loc.filter((part) => part !== "body").join(".");
    document.querySelectorAll<HTMLElement>(`[data-loc="${key}"]`).forEach((node) => {
      node.classList.add("invalid");
    });
  }
}

function detailText(payload: ErrorPayload): string {
  if (typeof payload.detail === "string") {
    return payload.detail;
  }
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((issue) => `${issue.loc.join(".")}：${issue.msg}`)
      .join("；");
  }
  return "请求未完成。";
}

/**
 * FastAPI 的 HTTPException(detail={...}) 会把整个业务错误体塞在顶层 detail 里，
 * 而全局异常处理器用的是顶层 {code, detail} 结构，422 则是 detail 数组。
 * 统一在这里摊平，界面不必为三种响应形状各写一份分支。
 */
function normalizeErrorPayload(payload: unknown): ErrorPayload {
  if (typeof payload !== "object" || payload === null) {
    return {};
  }
  const raw = payload as Record<string, unknown>;
  const nested = raw.detail;
  if (typeof nested === "object" && nested !== null && !Array.isArray(nested)) {
    return nested as ErrorPayload;
  }
  return raw as ErrorPayload;
}

function isReplayReport(
  report: ReplayReport | IntentEvalReportPayload | undefined,
): report is ReplayReport {
  return report !== undefined && Array.isArray((report as ReplayReport).results);
}

function intentGateDetails(payload: ErrorPayload): string[] {
  if (isReplayReport(payload.report) || payload.report === undefined) {
    return [];
  }
  const items: string[] = [];
  for (const row of payload.report.coverage ?? []) {
    const have = row.have ?? 0;
    const need = row.need ?? 0;
    if (have < need) {
      const label = row.label ?? row.category ?? "样例";
      items.push(`${label}不足：${have}/${need}，还缺 ${need - have} 条`);
    }
  }
  for (const finding of payload.report.findings ?? []) {
    if (finding.detail !== undefined) {
      items.push(finding.detail);
    }
  }
  return items;
}

/** 把服务端门禁的权威结论写在样例状态条上：缺哪一类、缺几条以它为准。 */
function renderIntentGate(payload: ErrorPayload): void {
  const report = payload.report;
  const items = intentGateDetails(payload);
  if (items.length === 0 || report === undefined || isReplayReport(report)) {
    return;
  }
  const box = el<HTMLDivElement>("sample-status");
  box.className = "warn";
  box.innerHTML =
    `<strong>服务端发布门禁拦下（权威结论，状态 ${escapeHtml(report.status ?? "fail")}）</strong>` +
    `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` +
    "<div>界面本地条数只是提示；请按这里的缺口补齐后重新发布。</div>";
}

function showFailure(error: unknown): void {
  if (error instanceof ApiFailure) {
    const payload = error.payload;
    const problems: FieldProblem[] = payload.fields ??
      (Array.isArray(payload.detail)
        ? payload.detail.map((issue) => ({
            loc: issue.loc.filter((part) => part !== "body"),
            message: issue.msg,
          }))
        : []);
    if (problems.length > 0) {
      markInvalid(problems);
    }
    const entries = [
      ...problems.map(
        (item) => `${item.loc.join(".")}：${item.message}`,
      ),
      ...intentGateDetails(payload),
    ];
    const list = entries.length > 0
      ? `<ul>${entries.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
      : "";
    showMessage(
      "err",
      `<strong>[${error.status}] ${escapeHtml(payload.code ?? "request_failed")}</strong> ` +
        `${escapeHtml(detailText(payload))}${list}`,
    );
    renderIntentGate(payload);
  } else {
    showMessage("err", escapeHtml(String(error)));
  }
}

async function api<T>(path: string, method: string = "GET", body?: unknown): Promise<T> {
  const token = value("admin-token");
  const base = (value("api-base") || location.origin).replace(/\/+$/, "");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token !== "") {
    headers["X-WorkPanda-Admin-Token"] = token;
  }
  const response = await fetch(base + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload: unknown = null;
  if (text !== "") {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!response.ok) {
    throw new ApiFailure(response.status, normalizeErrorPayload(payload));
  }
  return payload as T;
}

/** 统一包一层：任何失败都渲染成明确错误，绝不留下"看起来成功了"的界面。 */
async function guarded(action: () => Promise<void>): Promise<void> {
  try {
    await action();
  } catch (error) {
    showFailure(error);
  }
}

// ---------- 版本列表 ----------

function actionButton(
  label: string,
  onClick: () => Promise<void>,
  extra?: string,
): HTMLButtonElement {
  const button = document.createElement("button");
  button.textContent = label;
  if (extra !== undefined) {
    button.className = extra;
  }
  button.addEventListener("click", () => void guarded(onClick));
  return button;
}

function renderFlows(flows: FlowSummary[]): void {
  const tbody = el<HTMLTableSectionElement>("flow-list");
  tbody.replaceChildren();
  if (flows.length === 0) {
    const row = tbody.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 6;
    cell.className = "muted";
    cell.textContent = "版本库为空，先保存一份草稿。";
    return;
  }
  for (const flow of flows) {
    const row = tbody.insertRow();
    for (const text of [flow.flow_id, flow.version, flow.title]) {
      row.insertCell().textContent = text;
    }
    const stateCell = row.insertCell();
    const tag = document.createElement("span");
    if (flow.published) {
      tag.className = "tag on";
      // 署名直接印在状态上：这一列回答的第一问题不是"发布了没有"，而是"谁推上去的"。
      tag.textContent =
        flow.published_by === null ? "已发布" : `已发布 · ${flow.published_by}`;
      if (flow.updated_at !== null) {
        tag.title = `发布于 ${flow.updated_at}`;
      }
    } else {
      tag.className = "tag off";
      tag.textContent = "草稿";
    }
    stateCell.replaceChildren(tag);
    if (flow.disabled_by !== null) {
      const note = document.createElement("div");
      note.className = "muted";
      note.textContent = `由 ${flow.disabled_by} 停用`;
      stateCell.append(note);
    }
    // 第 5 列：用例数。
    row.insertCell().textContent = String(flow.case_count);

    const actions = row.insertCell();
    const container = document.createElement("div");
    container.className = "actions";
    container.append(
      actionButton("载入编辑", () => loadIntoDraft(flow)),
      actionButton("查看用例", () => loadCases(flow)),
      actionButton("回放", () => runReplay(flow)),
      actionButton("发布", () => publish(flow, true), "primary"),
      actionButton("回滚到此版本", () => rollback(flow), "danger"),
    );
    actions.append(container);
  }
}

async function refreshFlows(): Promise<void> {
  const flows = await api<FlowSummary[]>("/api/admin/flows");
  renderFlows(flows);
  showMessage("ok", `已读取 ${flows.length} 个版本。`);
}

// ---------- 草稿表单 ----------

const STEP_TEMPLATE = `
  <div class="field"><label>step_id</label><input data-key="step_id" data-loc="steps.IDX.step_id" /></div>
  <div class="field"><label>类型</label><select data-key="kind" data-loc="steps.IDX.kind">
    <option value="collect">collect</option><option value="execute">execute</option>
    <option value="handoff">handoff</option></select></div>
  <div class="field"><label>标题</label><input data-key="title" data-loc="steps.IDX.title" /></div>
  <div class="field"><label>必填字段（逗号分隔）</label><input data-key="required_fields" data-loc="steps.IDX.required_fields" /></div>
  <div class="field"><label>prompt</label><input data-key="prompt" data-loc="steps.IDX.prompt" /></div>
  <div class="field"><label>instruction</label><input data-key="instruction" data-loc="steps.IDX.instruction" /></div>
  <div class="field"><label>tool</label><input data-key="tool" data-loc="steps.IDX.tool" /></div>
  <div class="field"><label>confirm_message</label><input data-key="confirm_message" data-loc="steps.IDX.confirm_message" /></div>
  <div class="field"><label>需确认</label><input type="checkbox" data-key="requires_confirmation" data-loc="steps.IDX.requires_confirmation" /></div>
  <div class="field"><label></label><button type="button" data-remove="1" class="danger">删除</button></div>
`;

function renumberSteps(): void {
  document.querySelectorAll<HTMLElement>("#steps .step").forEach((row, index) => {
    row.querySelectorAll<HTMLElement>("[data-loc]").forEach((input) => {
      const key = input.dataset.loc?.split(".").pop() ?? "";
      input.dataset.loc = `steps.${index}.${key}`;
    });
  });
}

function addStepRow(initial?: Partial<StepInput>): void {
  const row = document.createElement("div");
  row.className = "step";
  row.innerHTML = STEP_TEMPLATE.replace(/IDX/g, "__NEW__");
  const index = el<HTMLDivElement>("steps").children.length;
  row.querySelectorAll<HTMLElement>("[data-loc]").forEach((input) => {
    const key = input.dataset.loc?.split(".").pop() ?? "";
    input.dataset.loc = `steps.${index}.${key}`;
  });
  if (initial !== undefined) {
    for (const [key, raw] of Object.entries(initial)) {
      const input = row.querySelector<HTMLInputElement | HTMLSelectElement>(
        `[data-key="${key}"]`,
      );
      if (input === null) {
        continue;
      }
      if (input instanceof HTMLInputElement && input.type === "checkbox") {
        input.checked = Boolean(raw);
      } else if (Array.isArray(raw)) {
        input.value = raw.join(",");
      } else if (raw !== null && raw !== undefined) {
        input.value = String(raw);
      }
    }
  }
  row.querySelector<HTMLButtonElement>("[data-remove]")?.addEventListener("click", () => {
    row.remove();
    renumberSteps();
  });
  el<HTMLDivElement>("steps").append(row);
}

function readStep(row: Element): StepInput {
  const read = (key: string): string => {
    const input = row.querySelector<HTMLInputElement | HTMLSelectElement>(
      `[data-key="${key}"]`,
    );
    return input === null ? "" : input.value.trim();
  };
  const optional = (key: string): string | undefined => {
    const text = read(key);
    return text === "" ? undefined : text;
  };
  const checkbox = row.querySelector<HTMLInputElement>(
    '[data-key="requires_confirmation"]',
  );
  return {
    step_id: read("step_id"),
    kind: read("kind") as StepKind,
    title: read("title"),
    required_fields: read("required_fields")
      .split(/[,，]/)
      .map((item) => item.trim())
      .filter((item) => item !== ""),
    prompt: optional("prompt"),
    instruction: optional("instruction"),
    tool: optional("tool"),
    confirm_message: optional("confirm_message"),
    requires_confirmation: checkbox?.checked ?? false,
  };
}

// ---------- 意图样例：动态列表与本地预检 ----------

const SAMPLE_TEMPLATE = `
  <div class="field"><label>sample_id</label><input data-key="sample_id" data-loc="intent_samples.IDX.sample_id" /></div>
  <div class="field"><label>类别</label><select data-key="category" data-loc="intent_samples.IDX.category">
    <option value="positive">正例</option><option value="near_miss">近似误例</option>
    <option value="out_of_scope">超范围例</option></select></div>
  <div class="field"><label>话术（合成 / 脱敏）</label><input data-key="text" data-loc="intent_samples.IDX.text" /></div>
  <div class="field"><label></label><button type="button" data-remove="1" class="danger">删除</button></div>
`;

function renumberSamples(): void {
  document.querySelectorAll<HTMLElement>("#samples .sample").forEach((row, index) => {
    row.querySelectorAll<HTMLElement>("[data-loc]").forEach((input) => {
      const key = input.dataset.loc?.split(".").pop() ?? "";
      input.dataset.loc = `intent_samples.${index}.${key}`;
    });
  });
}

function addSampleRow(category: SampleCategory, initial?: Partial<SampleInput>): void {
  const row = document.createElement("div");
  row.className = "sample";
  row.innerHTML = SAMPLE_TEMPLATE.replace(/IDX/g, "__NEW__");
  const index = el<HTMLDivElement>("samples").children.length;
  row.querySelectorAll<HTMLElement>("[data-loc]").forEach((input) => {
    const key = input.dataset.loc?.split(".").pop() ?? "";
    input.dataset.loc = `intent_samples.${index}.${key}`;
  });
  const categoryInput = row.querySelector<HTMLSelectElement>('[data-key="category"]');
  if (categoryInput !== null) {
    categoryInput.value = category;
  }
  if (initial !== undefined) {
    for (const [key, raw] of Object.entries(initial)) {
      const input = row.querySelector<HTMLInputElement | HTMLSelectElement>(
        `[data-key="${key}"]`,
      );
      if (input !== null && raw !== null && raw !== undefined) {
        input.value = String(raw);
      }
    }
  }
  row.querySelector<HTMLButtonElement>("[data-remove]")?.addEventListener("click", () => {
    row.remove();
    renumberSamples();
    refreshSampleGate();
  });
  el<HTMLDivElement>("samples").append(row);
}

function readSample(row: Element): SampleInput {
  const read = (key: string): string => {
    const input = row.querySelector<HTMLInputElement | HTMLSelectElement>(
      `[data-key="${key}"]`,
    );
    return input === null ? "" : input.value.trim();
  };
  return {
    sample_id: read("sample_id"),
    category: read("category") as SampleCategory,
    text: read("text"),
  };
}

/** 每次输入都重算：按钮禁用与「缺哪一类、缺几条」由同一份纯函数给出。 */
function refreshSampleGate(): void {
  const samples = Array.from(document.querySelectorAll("#samples .sample")).map(readSample);
  const decision = sampleGateDecision(checkSamples(samples));
  const box = el<HTMLDivElement>("sample-status");
  box.className = decision.className;
  box.innerHTML = decision.html;
  el<HTMLButtonElement>("save-draft").disabled = decision.disabled;
}

/** 默认摆出正好够下限的空行，让缺口一眼可见；全空行不报形状错误。 */
function resetSamples(): void {
  el<HTMLDivElement>("samples").replaceChildren();
  for (const category of SAMPLE_CATEGORIES) {
    const need = category === "positive" ? 3 : category === "near_miss" ? 2 : 1;
    for (let index = 0; index < need; index += 1) {
      addSampleRow(category);
    }
  }
  refreshSampleGate();
}

function loadSamples(samples: SampleInput[]): void {
  el<HTMLDivElement>("samples").replaceChildren();
  for (const sample of samples) {
    if (!SAMPLE_CATEGORIES.includes(sample.category)) {
      continue;
    }
    addSampleRow(sample.category, sample);
  }
  refreshSampleGate();
}

function readDraft(): FlowDraft {
  return {
    flow_id: value("draft-flow"),
    version: value("draft-version"),
    title: value("draft-title"),
    steps: Array.from(document.querySelectorAll("#steps .step")).map(readStep),
    intent_samples: Array.from(document.querySelectorAll("#samples .sample")).map(readSample),
  };
}

async function saveDraft(): Promise<void> {
  clearInvalid();
  const payload = readDraft();
  const gate = checkSamples(payload.intent_samples);
  if (!gate.ready) {
    // 按钮已禁用；这里再兜一次，避免绕过 DOM（控制台、脚本）直接触发保存。
    refreshSampleGate();
    const reasons = [
      ...gate.shortfalls,
      ...gate.problems.map((problem) => `${problem.loc}：${problem.message}`),
    ];
    showMessage("err", `本地预检未通过，草稿未提交：${escapeHtml(reasons.join("；"))}`);
    return;
  }
  const created = await api<FlowDraft>("/api/admin/flows", "POST", payload);
  showMessage("ok", `草稿 ${escapeHtml(created.flow_id)}@${escapeHtml(created.version)} 已保存（未发布）。`);
  await refreshFlows();
}

async function loadIntoDraft(flow: FlowSummary): Promise<void> {
  const detail = await api<VersionDetail>(
    `/api/admin/flows/${encodeURIComponent(flow.flow_id)}/versions/${encodeURIComponent(flow.version)}`,
  );
  el<HTMLInputElement>("draft-flow").value = detail.flow.flow_id;
  // 载入已保存版本只做查看，另存时必须换版本号（版本不可变）。
  el<HTMLInputElement>("draft-version").value = "";
  el<HTMLInputElement>("draft-title").value = detail.flow.title;
  const steps = el<HTMLDivElement>("steps");
  steps.replaceChildren();
  for (const step of detail.flow.steps) {
    addStepRow(step as unknown as Partial<StepInput>);
  }
  loadSamples(detail.flow.intent_samples ?? []);
  showMessage("ok", `已载入 ${escapeHtml(flow.flow_id)}@${escapeHtml(flow.version)}；另存请填写新版本号。`);
}

// ---------- 用例与回放 ----------

function versionPath(): string {
  const flowId = value("case-flow");
  const version = value("case-version");
  if (flowId === "" || version === "") {
    throw new Error("请先填写 flow_id 与 version。");
  }
  return `/api/admin/flows/${encodeURIComponent(flowId)}/versions/${encodeURIComponent(version)}`;
}

function splitList(raw: string): string[] {
  return raw.split(/[,，]/).map((item) => item.trim()).filter((item) => item !== "");
}

async function addCase(): Promise<void> {
  let slots: Record<string, string> = {};
  const rawSlots = value("case-slots");
  if (rawSlots !== "") {
    const parsed: unknown = JSON.parse(rawSlots);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("slots 必须是 JSON 对象。");
    }
    slots = parsed as Record<string, string>;
  }
  const steps = splitList(value("case-steps"));
  const tools = splitList(value("case-tools"));
  const terminal = value("case-terminal");
  const expect: Record<string, unknown> = {};
  if (value("case-steps") !== "") {
    expect.steps = steps;
  }
  if (value("case-tools") !== "") {
    expect.tools = tools;
  }
  if (terminal !== "") {
    expect.terminal = terminal;
  }
  const created = await api<ReplayCaseView>(`${versionPath()}/cases`, "POST", {
    case_id: value("case-id"),
    slots,
    expect,
  });
  showMessage("ok", `用例 ${escapeHtml(created.case_id)} 已保存。`);
  await loadCases({ flow_id: value("case-flow"), version: value("case-version") });
}

function renderCases(cases: ReplayCaseView[], path: string): void {
  const box = el<HTMLDivElement>("cases");
  box.replaceChildren();
  if (cases.length === 0) {
    box.innerHTML = '<p class="muted">该版本还没有用例（没有基线时发布门禁会如实标注 replay_ran=false）。</p>';
    return;
  }
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const label of ["case_id", "slots", "期望", "操作"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    head.append(cell);
  }
  const body = table.createTBody();
  for (const item of cases) {
    const row = body.insertRow();
    row.insertCell().textContent = item.case_id;
    row.insertCell().textContent = JSON.stringify(item.slots);
    row.insertCell().textContent = JSON.stringify(item.expect);
    const cell = row.insertCell();
    cell.append(
      actionButton("删除", async () => {
        await api(`${path}/cases/${encodeURIComponent(item.case_id)}`, "DELETE");
        showMessage("ok", `用例 ${escapeHtml(item.case_id)} 已删除。`);
        await loadCases({ flow_id: value("case-flow"), version: value("case-version") });
      }, "danger"),
    );
  }
  box.append(table);
}

async function loadCases(flow: { flow_id: string; version: string }): Promise<void> {
  el<HTMLInputElement>("case-flow").value = flow.flow_id;
  el<HTMLInputElement>("case-version").value = flow.version;
  const detail = await api<VersionDetail>(versionPath());
  renderCases(detail.cases, versionPath());
  showMessage(
    "ok",
    `${escapeHtml(flow.flow_id)}@${escapeHtml(flow.version)}：${detail.cases.length} 条用例；` +
      `当前${detail.published ? "已发布" : "未发布"}。`,
  );
}

function renderReport(report: ReplayReport): void {
  const box = el<HTMLDivElement>("replay-result");
  const verdict = escapeHtml(report.verdict);
  const rows = report.results
    .map((result) => {
      const steps = result.steps
        .map((step) => `${escapeHtml(step.step_id)}=${escapeHtml(step.outcome)}`)
        .join(" → ");
      const findings = result.findings
        .map((item) => `${escapeHtml(item.code)}：${escapeHtml(item.detail)}`)
        .join("；");
      return `<tr><td>${escapeHtml(result.case_id)}</td><td>${escapeHtml(result.verdict)}</td>` +
        `<td class="mono">${steps || "（无）"}</td><td>${findings || "（无）"}</td></tr>`;
    })
    .join("");
  box.innerHTML =
    `<p>总判定：<strong>${verdict}</strong>（${escapeHtml(report.flow_id)}@` +
    `${escapeHtml(report.flow_version)}）</p>` +
    `<table><thead><tr><th>用例</th><th>判定</th><th>逐 step 结果</th><th>发现</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>` +
    '<p class="muted">回放只说明步骤与工具是否符合预期，不代表真实业务系统已办成。</p>';
}

async function runReplay(flow: { flow_id: string; version: string }): Promise<void> {
  el<HTMLInputElement>("case-flow").value = flow.flow_id;
  el<HTMLInputElement>("case-version").value = flow.version;
  const report = await api<ReplayReport>(`${versionPath()}/replay`, "POST");
  renderReport(report);
  showMessage("ok", `回放完成，总判定 ${escapeHtml(report.verdict)}。`);
}

// ---------- 发布指针 ----------

function actionTarget(): { flow_id: string; version: string } {
  const flowId = value("action-flow");
  const version = value("action-version");
  if (flowId === "" || version === "") {
    throw new Error("请先填写 flow_id 与 version。");
  }
  return { flow_id: flowId, version };
}

async function publish(
  flow: { flow_id: string; version: string },
  apply: boolean,
): Promise<void> {
  el<HTMLInputElement>("action-flow").value = flow.flow_id;
  el<HTMLInputElement>("action-version").value = flow.version;
  try {
    const outcome = await api<PublishOutcome>(
      `/api/admin/flows/${encodeURIComponent(flow.flow_id)}/publish`,
      "POST",
      { version: flow.version, apply },
    );
    // 署名以服务端返回为准：界面不自己拼"当前登录人"，否则展示出来的名字可能
    // 与库里记下的主体不是同一个（预演更是根本没有署名）。
    const attribution =
      outcome.published_by === null
        ? ""
        : `，署名 ${escapeHtml(outcome.published_by)}`;
    showMessage(
      "ok",
      `<strong>${outcome.published ? "已发布" : "预演通过，未发布"}</strong>：` +
        `${escapeHtml(outcome.message)}${attribution}；回放门禁${outcome.replay_ran ? "已执行" : "未执行（该版本没有用例）"}。`,
    );
    await refreshFlows();
  } catch (error) {
    // 回放门禁与识别样例门禁的 409 报告结构不同：只有前者能渲染成逐 step 表格，
    // 后者由 showFailure()/renderIntentGate() 写成「缺哪一类、缺几条」。
    if (error instanceof ApiFailure && isReplayReport(error.payload.report)) {
      renderReport(error.payload.report);
    }
    throw error;
  }
}

async function rollback(flow: { flow_id: string; version: string }): Promise<void> {
  const result = await api<RollbackOutcome>(
    `/api/admin/flows/${encodeURIComponent(flow.flow_id)}/rollback`,
    "POST",
    { version: flow.version },
  );
  showMessage(
    "ok",
    `已回滚 ${escapeHtml(flow.flow_id)} 到 ${escapeHtml(flow.version)}（署名 ${escapeHtml(result.published_by)}）；在途任务仍按各自版本继续。`,
  );
  await refreshFlows();
}

async function disable(): Promise<void> {
  const flowId = value("action-flow");
  if (flowId === "") {
    throw new Error("请先填写 flow_id。");
  }
  const result = await api<DisableOutcome>(
    `/api/admin/flows/${encodeURIComponent(flowId)}/disable`,
    "POST",
  );
  showMessage(
    "ok",
    `${escapeHtml(flowId)} 已由 ${escapeHtml(result.disabled_by)} 停用（历史版本与发布署名保留，可随时回滚）。`,
  );
  await refreshFlows();
}

// ---------- 初始化 ----------

function bind(id: string, action: () => Promise<void>): void {
  el<HTMLButtonElement>(id).addEventListener("click", () => void guarded(action));
}

el<HTMLInputElement>("api-base").value = location.origin;
addStepRow({ step_id: "collect", kind: "collect", title: "收集信息" });
addStepRow({ step_id: "submit", kind: "execute", title: "提交", tool: "example.tool" });
resetSamples();

const samplesBox = el<HTMLDivElement>("samples");
samplesBox.addEventListener("input", refreshSampleGate);
samplesBox.addEventListener("change", refreshSampleGate);
for (const [id, category] of [
  ["add-sample-positive", "positive"],
  ["add-sample-near-miss", "near_miss"],
  ["add-sample-out-of-scope", "out_of_scope"],
] as Array<[string, SampleCategory]>) {
  el<HTMLButtonElement>(id).addEventListener("click", () => {
    addSampleRow(category);
    refreshSampleGate();
  });
}

bind("refresh", refreshFlows);
bind("save-draft", saveDraft);
bind("add-case", addCase);
bind("load-cases", () => loadCases(actionTargetForCases()));
bind("run-replay", () => runReplay(actionTargetForCases()));
bind("dry-run", () => publish(actionTarget(), false));
bind("do-publish", () => publish(actionTarget(), true));
bind("do-rollback", () => rollback(actionTarget()));
bind("do-disable", disable);

function actionTargetForCases(): { flow_id: string; version: string } {
  return { flow_id: value("case-flow"), version: value("case-version") };
}

el<HTMLButtonElement>("add-step").addEventListener("click", () => {
  addStepRow();
});
el<HTMLButtonElement>("clear-draft").addEventListener("click", () => {
  for (const id of ["draft-flow", "draft-version", "draft-title"]) {
    el<HTMLInputElement>(id).value = "";
  }
  el<HTMLDivElement>("steps").replaceChildren();
  addStepRow();
  resetSamples();
  clearInvalid();
  showMessage("ok", "表单已清空（含意图样例）。");
});
