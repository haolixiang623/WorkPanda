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
  finishedRequest,
  panelHasContent,
  screenOf,
  transcriptOf,
  type Action,
  type ActionKind,
  type Connection,
  type Field,
  type Message,
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
const streamEl = must("stream");
const panelEl = must("panel");
const composerEl = must("composer");

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
  // 渲染会重建面板。先把已经敲进去的话收起来，否则"点发送"那一下
  // 会把人刚写的整段描述清掉。输入框现在是常驻节点，但文本仍归 `keepText` 管：
  // 它是"页面自己的东西"里唯一的例外，只在提交前存活。
  const composer = document.getElementById("composer-text");
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
  if (kind === "i_finished") {
    // 「我已完成」：只有 values，没有 step_id/accepted。它是在说"我去做过了"，
    // 不是"我批准这个提交"——两句话的后果完全不同，请求体因此必须不同。
    view = await request<TaskView>(
      `/api/tasks/${view.task_id}/answers`,
      finishedRequest(),
    );
    editing = null;
    keepText = "";
    return;
  }
  if (kind === "answer") {
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
  const composer = document.getElementById("composer-text");
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

/**
 * 把派生出来的对话记录同步到 `#stream`。
 *
 * 三条规则，都是为了"重渲染不重播"：
 *
 * 1. **按 `id` 复用节点**：同一条消息第二次出现时不重建，只改文本。`id` 由
 *    `transcriptOf` 从 `(状态, 步骤, 字段)` 决定，所以它对同一份视图是稳定的。
 * 2. **消息只会变长**：服务端的派生结果只会追加或原地改写（同一个字段从"待填"
 *    变成"已答"是 `q:...:open` 退场、`q:...` 入场），不会把中间那段删掉重排。
 *    因此不清空容器——清空就等于把整段历史重播一遍，滚动位置也跟着跳。
 * 3. **只在追加时滚到底**：人在往上翻旧消息时不要把他拽回来。
 */
function syncStream(messages: Message[]): void {
  const wanted = new Set(messages.map((message) => message.id));
  // 先删掉这次不该出现的（例如"返回修改"撤回了当前字段的追问）。
  for (const node of Array.from(streamEl.children)) {
    const id = (node as HTMLElement).dataset["msg"] ?? "";
    if (!wanted.has(id)) node.remove();
  }
  const atBottom =
    streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 24;
  const existing = new Map<string, HTMLElement>();
  for (const node of Array.from(streamEl.children)) {
    const element = node as HTMLElement;
    existing.set(element.dataset["msg"] ?? "", element);
  }
  let appended = false;
  for (const message of messages) {
    let node = existing.get(message.id);
    if (node === undefined) {
      node = messageNode(message);
      streamEl.appendChild(node);
      appended = true;
      continue;
    }
    if (node.textContent !== message.text) node.textContent = message.text;
  }
  if (appended || atBottom) streamEl.scrollTop = streamEl.scrollHeight;
}

function messageNode(message: Message): HTMLElement {
  const row = el("div", `msg ${message.kind} ${message.role}`);
  row.dataset["msg"] = message.id;
  row.appendChild(el("div", "bubble", message.text));
  return row;
}

// ---------- 渲染 ----------

function render(): void {
  renderConnection();
  noticeEl.className = notice === null ? "" : "notice";
  noticeEl.textContent = notice ?? "";

  const screen = screenOf(view);
  // 修改态只属于确认页；状态一变就退出，避免本地模式跨状态残留。
  if (screen.state !== "awaiting_confirmation") editing = null;

  // 消息流由服务端视图派生，页面不累积任何数组。
  syncStream(transcriptOf(view));

  panelEl.replaceChildren(statusCard(screen));
  // 没有可显示的内容就不给这块卡片：空卡片在手机上是块白板，看着像页面坏了。
  if (panelHasContent(screen)) panelEl.appendChild(panel(screen));
  renderComposer(screen);
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

  if (screen.state === "awaiting_action") {
    // 这一屏**没有输入框**：要用户做的是"去别处做一件事"，不是"在这里填什么"。
    // 多一个输入框，就多一处"用户以为该把验证码告诉我"的地方。
    box.appendChild(guideBox(screen));
  }

  // 办结屏**不在这里印结论**：`transcriptOf` 已经把它作为 `result` 消息放进对话流。
  // 两处都印的后果实测过——真实模型的三段式答案 300+ 字，在 390px 屏上出现两遍。

  if (screen.state === "failed" && view !== null && view.failure_code !== null) {
    // 失败原因不印 `failure_message`（那是工具/模型侧原话）。这里给的是"可以去哪里
    // 问"这一条可确认的事实，不是猜测的失败理由。
    box.appendChild(
      el("p", "muted", "如需要进一步说明，请携带受理编号联系办理单位。"),
    );
  }

  if (editing !== null) {
    // 修改态的操作条换成本地提交，避免同时出现"确认提交"和"保存修改"。
    return box;
  }
  box.appendChild(actionBar(screen));
  return box;
}

/**
 * 常驻输入框。**整页只建一次**，之后只切换启用态——这样重渲染不会清掉人正在
 * 敲的字，焦点也不会每收一条 SSE 提醒就丢一次。
 *
 * "现在不能输入"不是一个按钮的禁用态，而是一句话：群众要能看出为什么打不了字，
 * 而不是对着一个灰框发呆。所以禁用时把原因写进**标签**（每个状态都有话说），
 * 占位符只在真有下一步可指的时候指路——终态下没有下一步，空着比写句空话强。
 */
function renderComposer(screen: Screen): void {
  const active =
    (screen.state === "idle" || screen.state === "needs_clarification") && !busy;

  if (composerEl.childElementCount === 0) {
    const label = el("label", "", "请描述您要办理的事情");
    label.setAttribute("for", "composer-text");
    const text = el("textarea");
    text.id = "composer-text";
    text.rows = 2;
    text.maxLength = 2000;
    text.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const send = document.getElementById("composer-send");
        if (send instanceof HTMLButtonElement && !send.disabled) send.click();
      }
    });
    const send = el("button", "primary", "发送");
    send.id = "composer-send";
    send.type = "submit";
    composerEl.appendChild(label);
    composerEl.appendChild(text);
    composerEl.appendChild(send);
    composerEl.addEventListener("submit", (event) => {
      event.preventDefault();
      void perform("start");
    });
  }

  if (active) {
    (composerEl.firstElementChild as HTMLElement).textContent = "请描述您要办理的事情";
  } else {
    // 说"上面"不是笔误：要填的表单在 `#panel` 里，而输入区是页面最后一块，
    // 表单永远在它上方。原来这里写的是"请在下方补充这一项"，群众照着往下翻，
    // 下面除了页脚什么都没有。
    (composerEl.firstElementChild as HTMLElement).textContent = `现在不用您打字：${
      screen.state === "awaiting_input" ? "请在页面上方的表单里填写。" : screen.hint
    }`;
  }

  const text = document.getElementById("composer-text");
  if (text instanceof HTMLTextAreaElement) {
    text.disabled = !active;
    // 占位符只在真有下一步可指的时候指路。终态（办结、待确认、转人工）下面既没有
    // 表单也没有按钮，写"请用下面的表单继续"是句空话——原因已经写在上面那行标签里。
    text.placeholder = active
      ? "例如：我想给家里老人办一下高龄补贴"
      : screen.state === "awaiting_input"
        ? "请填写上面的表单"
        : "";
    if (active) {
      // 只在值真的不同时才写：否则每收一条事件都会把光标顶到末尾。
      if (text.value !== keepText) text.value = keepText;
    } else if (!busy && text.value !== "") {
      // 用不了的时候把框清空。留着上一句话，那块置灰的框看着像"我这句话还没发出去"，
      // 群众会去反复点旁边同样置灰的「发送」；而"为什么打不了字"已经写在标签与
      // 占位符里了，不需要再靠残留的正文暗示。
      //
      // `busy` 例外：动作用户正在发、结果还没回来那一下先别清——万一失败，草稿要原样
      // 回到框里，先清后填会闪一下。
      text.value = "";
    }
  }
  const send = document.getElementById("composer-send");
  if (send instanceof HTMLButtonElement) send.disabled = !active;
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
      const change = actionButton({ kind: "edit", label: "修改", primary: false }, () => {
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
  if (action.kind === "i_finished") {
    // 不看输入框：这一屏没有要提交的字段，而"用户填了什么"本来就不该进系统。
    void perform("i_finished");
    return;
  }
  if (action.kind === "answer") {
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
