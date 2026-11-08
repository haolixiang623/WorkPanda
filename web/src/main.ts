/**
 * 群众侧最小对话页：交互层。
 *
 * 三条纪律（都有回归，见 `tests/test_web_ui.py`）：
 *
 * 1. **状态只有一个来源**：`GET /api/tasks/{id}` 返回的 `ui_state`。事件流只是
 *    "去问一次服务端"的提醒，从不直接改状态；页面也不按动画结束或请求超时推断
 *    完成——那类代码在这里没有入口。
 * 2. **服务端未受理的事，页面不先显示**：每次写入以服务端响应为准，失败则原样
 *    停在原状态并显示服务端给的话。
 * 3. **凭据与业务字段不落本地、不进 URL**：页面从不读 `location`，业务字段只存在
 *    内存与页面上；`sessionStorage` 里只有会话令牌、任务号与对话号三项，
 *    关掉标签页即失效。
 */

import {
  answerRequest,
  canSubmit,
  confirmRequest,
  connectionNotice,
  fieldOf,
  screenOf,
  type Action,
  type ActionKind,
  type Connection,
  type Field,
  type Screen,
  type TaskView,
} from "./state_view.js";

const SESSION_HEADER = "X-WorkPanda-Session";
const SESSION_KEY = "workpanda.session";
const TASK_KEY = "workpanda.task";
const CONVERSATION_KEY = "workpanda.conversation";
/** 自动重连上限。超过之后停在离线横幅上，由人决定再试。 */
const MAX_ATTEMPTS = 5;

const connectionEl = must("connection");
const noticeEl = must("notice");
const appEl = must("app");

let session = sessionStorage.getItem(SESSION_KEY) ?? "";
let taskId = sessionStorage.getItem(TASK_KEY) ?? "";
let conversationId = sessionStorage.getItem(CONVERSATION_KEY) ?? newConversationId();
sessionStorage.setItem(CONVERSATION_KEY, conversationId);

let view: TaskView | null = null;
let connection: Connection = "live";
/** 确认页「返回修改」选中的字段名。纯本地模式，不改变任何服务端状态。 */
let editing: string | null = null;
/** 本地提示（服务端原话或说明）。不是任务状态。 */
let notice: string | null = null;
let keepText = "";
let busy = false;
let lastSequence = 0;
let attempts = 0;
let streamToken = 0;
let streamAbort: AbortController | null = null;
let refreshTimer: number | null = null;

class ApiFailure extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly uiState: string | null,
  ) {
    super(message);
    this.name = "ApiFailure";
  }
}

const FALLBACK: Record<number, string> = {
  401: "会话已过期，请重新开始。",
  403: "没有权限执行这个操作。",
  404: "没有找到这次办理，可能已经过期。",
  409: "任务状态已变化，请刷新后重试。",
  429: "操作过于频繁，请稍后再试。",
  503: "服务暂时不可用，请稍后重试。",
};

function must(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`页面缺少节点 #${id}`);
  return node;
}

function newConversationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `conv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className !== undefined && className !== "") node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------- 与服务端交互 ----------

async function request<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (session !== "") headers[SESSION_HEADER] = session;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    credentials: "omit",
  });
  // 首次请求由服务端签发会话，随后每个请求都带上；令牌只放内存与 sessionStorage。
  const issued = response.headers.get(SESSION_HEADER);
  if (issued !== null && issued !== "") {
    session = issued;
    sessionStorage.setItem(SESSION_KEY, issued);
  }
  if (!response.ok) {
    const failure = await describeFailure(response);
    throw new ApiFailure(response.status, failure.code, failure.message, failure.uiState);
  }
  return (await response.json()) as T;
}

async function describeFailure(
  response: Response,
): Promise<{ code: string; message: string; uiState: string | null }> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const fallback = FALLBACK[response.status] ?? "请求未完成，请稍后重试。";
  if (typeof body !== "object" || body === null) {
    return { code: "", message: fallback, uiState: null };
  }
  const record = body as Record<string, unknown>;
  const detail = record["detail"];
  return {
    code: typeof record["code"] === "string" ? record["code"] : "",
    // 服务端给的是中文的群众口径文案；校验类错误（数组）用兜底文案，不把
    // pydantic 的原始结构印到页面上。
    message: typeof detail === "string" && detail !== "" ? detail : fallback,
    uiState: typeof record["ui_state"] === "string" ? record["ui_state"] : null,
  };
}

// ---------- 状态刷新与事件流 ----------

async function refresh(): Promise<void> {
  if (taskId === "") {
    view = null;
    render();
    return;
  }
  try {
    view = await request<TaskView>(`/api/tasks/${taskId}`);
    render();
  } catch (error) {
    fail(error);
  }
}

function scheduleRefresh(): void {
  if (refreshTimer !== null) return;
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null;
    void refresh();
  }, 120);
}

function connect(): void {
  closeStream();
  attempts = 0;
  if (taskId === "") return;
  void pump();
}

function closeStream(): void {
  streamToken += 1;
  if (streamAbort !== null) {
    streamAbort.abort();
    streamAbort = null;
  }
  if (refreshTimer !== null) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

async function pump(): Promise<void> {
  if (taskId === "") return;
  const token = ++streamToken;
  const controller = new AbortController();
  streamAbort = controller;
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (session !== "") headers[SESSION_HEADER] = session;

  let response: Response;
  try {
    // 事件流用 fetch 而不是 EventSource：`EventSource` 不能自定义请求头，
    // 而会话令牌正是走请求头的。
    response = await fetch(`/api/tasks/${taskId}/events?after=${lastSequence}`, {
      headers,
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal,
    });
  } catch {
    if (token === streamToken) reconnect();
    return;
  }
  if (token !== streamToken) return;
  if (!response.ok || response.body === null) {
    reconnect();
    return;
  }
  attempts = 0;
  setConnection("live");
  await readStream(response.body, token);
  if (token === streamToken) reconnect();
}

async function readStream(body: ReadableStream<Uint8Array>, token: number): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done || token !== streamToken) break;
      // sse-starlette 用 CRLF 分隔，先归一化再按空行切事件。
      buffer = (buffer + decoder.decode(value, { stream: true })).split("\r\n").join("\n");
      let index = buffer.indexOf("\n\n");
      while (index >= 0) {
        consume(buffer.slice(0, index));
        buffer = buffer.slice(index + 2);
        index = buffer.indexOf("\n\n");
      }
    }
  } catch {
    // 断流按重连处理；此时**不动**任务状态，页面上仍是最后一次权威结果。
  }
}

function consume(chunk: string): void {
  for (const line of chunk.split("\n")) {
    if (!line.startsWith("id:")) continue;
    const parsed = Number(line.slice(3).trim());
    if (Number.isFinite(parsed) && parsed > lastSequence) lastSequence = parsed;
  }
  // 事件本身不携带结论：收到什么都去问一次服务端。
  scheduleRefresh();
}

function reconnect(): void {
  if (taskId === "") return;
  attempts += 1;
  if (attempts >= MAX_ATTEMPTS) {
    setConnection("offline");
    return;
  }
  setConnection("reconnecting");
  const delay = Math.min(1000 * 2 ** (attempts - 1), 8000);
  window.setTimeout(() => void pump(), delay);
}

function setConnection(next: Connection): void {
  connection = next;
  render();
}

// ---------- 动作 ----------

async function perform(kind: ActionKind, field?: Field): Promise<void> {
  if (busy) return;
  busy = true;
  notice = null;
  // 渲染会重建输入框。先把已经敲进去的话收起来，否则"点开始办理"那一下
  // 会把人刚写的整段描述清掉。
  const composer = document.getElementById("composer");
  if (composer instanceof HTMLTextAreaElement) keepText = composer.value;
  render();
  try {
    await dispatch(kind, field);
  } catch (error) {
    fail(error);
  } finally {
    busy = false;
    render();
  }
}

async function dispatch(kind: ActionKind, field?: Field): Promise<void> {
  if (kind === "restart") {
    resetTask();
    return;
  }
  if (kind === "start") {
    await startTask();
    return;
  }
  if (view === null) return;
  if (kind === "answer" || kind === "i_finished") {
    if (field === undefined) return;
    view = await request<TaskView>(`/api/tasks/${view.task_id}/answers`, answerRequest(field));
    editing = null;
    keepText = "";
    return;
  }
  if (kind === "confirm") {
    view = await request<TaskView>(`/api/tasks/${view.task_id}/confirm`, confirmRequest(view));
    editing = null;
    return;
  }
  if (kind === "cancel") {
    view = await request<TaskView>(`/api/tasks/${view.task_id}/cancel`, {});
    return;
  }
}

async function startTask(): Promise<void> {
  const composer = document.getElementById("composer");
  const text =
    composer instanceof HTMLTextAreaElement ? composer.value.trim() : keepText.trim();
  if (text === "") {
    notice = "请先描述您要办理的事情。";
    render();
    return;
  }
  try {
    const created = await request<TaskView>("/api/tasks", {
      conversation_id: conversationId,
      input_text: text,
    });
    view = created;
    taskId = created.task_id;
    sessionStorage.setItem(TASK_KEY, taskId);
    keepText = "";
    connect();
  } catch (error) {
    if (error instanceof ApiFailure && error.uiState === "needs_clarification") {
      // 服务端没能确定事项、**没有**创建任务：把原话留在输入框里，让人改一句再说。
      keepText = text;
      notice = error.message;
      render();
      return;
    }
    throw error;
  }
}

function resetTask(): void {
  view = null;
  taskId = "";
  editing = null;
  lastSequence = 0;
  keepText = "";
  sessionStorage.removeItem(TASK_KEY);
  closeStream();
  connection = "live";
}

function fail(error: unknown): void {
  if (error instanceof ApiFailure) {
    if (error.status === 401) {
      session = "";
      sessionStorage.removeItem(SESSION_KEY);
      resetTask();
      notice = "会话已过期，请重新开始。";
      render();
      return;
    }
    if (error.status === 404) {
      resetTask();
      notice = "没有找到这次办理，可能已经过期。";
      render();
      return;
    }
    notice = error.message;
    render();
    if (error.status === 409) void refresh();
    return;
  }
  notice = "网络异常，请稍后重试。";
  reconnect();
}

// ---------- 渲染 ----------

function render(): void {
  appEl.replaceChildren();
  renderConnection();
  noticeEl.className = notice === null ? "" : "notice";
  noticeEl.textContent = notice ?? "";

  const screen = screenOf(view);
  // 修改态只属于确认页；状态一变就退出，避免本地模式跨状态残留。
  if (screen.state !== "awaiting_confirmation") editing = null;
  appEl.appendChild(statusCard(screen));
  appEl.appendChild(panel(screen));
}

function renderConnection(): void {
  const info = connectionNotice(connection);
  connectionEl.replaceChildren();
  if (info === null) {
    connectionEl.className = "connection";
    return;
  }
  connectionEl.className = `connection ${info.level}`;
  connectionEl.appendChild(el("span", "", info.text));
  if (info.level === "offline") {
    const retry = el("button", "link", "重试");
    retry.type = "button";
    retry.addEventListener("click", () => {
      attempts = 0;
      void pump();
    });
    connectionEl.appendChild(retry);
  }
}

function statusCard(screen: Screen): HTMLElement {
  const card = el("section", `card status ${screen.state}`);
  card.dataset["state"] = screen.state;
  card.appendChild(el("h2", "status-title", screen.title));
  card.appendChild(el("p", "hint", screen.hint));
  if (view !== null && view.completed_steps.length > 0 && !screen.terminal) {
    card.appendChild(el("p", "muted", `已完成 ${view.completed_steps.length} 个步骤`));
  }
  return card;
}

function panel(screen: Screen): HTMLElement {
  const box = el("section", "card panel");

  if (screen.state === "idle" || screen.state === "needs_clarification") {
    box.appendChild(composer());
  }

  if (screen.state === "awaiting_input" && screen.field !== null) {
    box.appendChild(fieldForm(screen.field));
    box.appendChild(summaryList(screen, false));
  }

  if (screen.state === "awaiting_confirmation") {
    if (editing !== null && view !== null) {
      box.appendChild(fieldForm(fieldOf(view, editing)));
      box.appendChild(editFooter());
      return box;
    }
    if (view !== null && view.current_step_title !== null) {
      box.appendChild(el("p", "action-target", `将要执行：${view.current_step_title}`));
    }
    box.appendChild(summaryList(screen, true));
    if (screen.impact !== null) {
      const impact = el("div", "impact");
      impact.appendChild(el("strong", "", "影响："));
      impact.appendChild(el("span", "", screen.impact));
      box.appendChild(impact);
    }
  }

  if (screen.state === "user_action_required") {
    box.appendChild(guideBox(screen));
    if (screen.field !== null) {
      box.appendChild(fieldForm(screen.field));
    }
  }

  if (screen.state === "completed" && screen.summary.length > 0) {
    box.appendChild(summaryList(screen, false));
  }

  if (editing !== null) {
    // 修改态的操作条换成本地提交，避免同时出现"确认提交"和"保存修改"。
    return box;
  }
  box.appendChild(actionBar(screen));
  return box;
}

function composer(): HTMLElement {
  const wrapper = el("div", "composer");
  const label = el("label", "", "请描述您要办理的事情");
  label.setAttribute("for", "composer");
  const area = el("textarea");
  area.id = "composer";
  area.rows = 3;
  area.maxLength = 2000;
  area.placeholder = "例如：我想给家里老人办一下高龄补贴";
  if (keepText !== "") area.value = keepText;
  wrapper.appendChild(label);
  wrapper.appendChild(area);
  return wrapper;
}

function fieldForm(field: Field): HTMLElement {
  const wrapper = el("div", "composer");
  const label = el("label", "", field.label);
  label.setAttribute("for", "field-input");
  const input = el("input");
  input.id = "field-input";
  input.type = "text";
  input.autocomplete = "off";
  input.maxLength = 200;
  // 值写进 value 属性会落到 DOM 上可被读取；这里只写属性对象（property）。
  input.value = field.value;
  wrapper.appendChild(label);
  wrapper.appendChild(input);
  return wrapper;
}

function editFooter(): HTMLElement {
  const row = el("div", "actions");
  const save = actionButton(
    { kind: "answer", label: "保存修改", primary: true },
    () => submitField(editing ?? undefined),
  );
  const back = actionButton({ kind: "edit", label: "不改了", primary: false }, () => {
    editing = null;
    render();
  });
  row.appendChild(save);
  row.appendChild(back);
  return row;
}

function guideBox(screen: Screen): HTMLElement {
  const list = el("dl", "guide");
  const guide = screen.guide;
  if (guide === null) return list;
  const add = (term: string, value: string): void => {
    list.appendChild(el("dt", "", term));
    list.appendChild(el("dd", "", value));
  };
  add("要做什么", guide.what);
  add("去哪里", guide.where);
  add("有效期", guide.expires);
  return list;
}

function summaryList(screen: Screen, editable: boolean): HTMLElement {
  const list = el("dl", "summary");
  for (const row of screen.summary) {
    list.appendChild(el("dt", "", row.label));
    const cell = el("dd", "", row.value);
    if (editable) {
      const change = actionButton({ kind: "edit", label: "改", primary: false }, () => {
        editing = row.name;
        render();
      });
      cell.appendChild(change);
    }
    list.appendChild(cell);
  }
  return list;
}

function actionBar(screen: Screen): HTMLElement {
  const row = el("div", "actions");
  for (const action of screen.actions) {
    const disabled = screen.blockedReason !== null && action.primary;
    row.appendChild(actionButton(action, () => onClick(action, screen), disabled));
  }
  if (screen.blockedReason !== null) {
    row.appendChild(el("p", "muted", screen.blockedReason));
  }
  return row;
}

function onClick(action: Action, screen: Screen): void {
  if (action.kind === "answer" || action.kind === "i_finished") {
    submitField(screen.field?.name);
    return;
  }
  if (action.kind === "edit") {
    editing = screen.summary.length > 0 ? screen.summary[0]?.name ?? null : null;
    render();
    return;
  }
  void perform(action.kind);
}

function submitField(name: string | undefined): void {
  if (name === undefined || view === null) return;
  const input = document.getElementById("field-input");
  const value = input instanceof HTMLInputElement ? input.value : "";
  const field: Field = { name, label: fieldOf(view, name).label, value };
  if (!canSubmit(field)) {
    notice = "请先填写后再提交。";
    render();
    return;
  }
  void perform("answer", field);
}

function actionButton(
  action: Action,
  handler: () => void,
  disabled = false,
): HTMLButtonElement {
  const button = el("button", action.primary ? "primary" : "", action.label);
  button.type = "button";
  button.disabled = disabled || busy;
  button.addEventListener("click", handler);
  return button;
}

// ---------- 启动 ----------

async function boot(): Promise<void> {
  render();
  if (taskId === "") return;
  try {
    // 刷新/重连都走这条路：按 task_id 从服务端恢复，不在本地重放任何步骤。
    view = await request<TaskView>(`/api/tasks/${taskId}`);
  } catch (error) {
    if (error instanceof ApiFailure && error.status === 401) {
      session = "";
      sessionStorage.removeItem(SESSION_KEY);
    }
    resetTask();
    notice =
      error instanceof ApiFailure && error.status === 404
        ? "上一次的办理已经结束或过期，请重新开始。"
        : "上一次的办理无法恢复，请重新开始。";
    render();
    return;
  }
  render();
  connect();
}

void boot();
