import { answerRequest, canSubmit, confirmRequest, connectionNotice, fieldOf, finishedRequest, panelHasContent, screenOf, transcriptOf, } from "./state_view.js";
const SESSION_HEADER = "X-WorkPanda-Session";
const SESSION_KEY = "workpanda.session";
const TASK_KEY = "workpanda.task";
const CONVERSATION_KEY = "workpanda.conversation";
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
let view = null;
let connection = "live";
let editing = null;
let notice = null;
let keepText = "";
let busy = false;
let lastSequence = 0;
let attempts = 0;
let streamToken = 0;
let streamAbort = null;
let refreshTimer = null;
class ApiFailure extends Error {
    constructor(status, code, message, uiState) {
        super(message);
        this.status = status;
        this.code = code;
        this.uiState = uiState;
        this.name = "ApiFailure";
    }
}
const FALLBACK = {
    401: "会话已过期，请重新开始。",
    403: "没有权限执行这个操作。",
    404: "没有找到这次办理，可能已经过期。",
    409: "任务状态已变化，请刷新后重试。",
    429: "操作过于频繁，请稍后再试。",
    503: "服务暂时不可用，请稍后重试。",
};
function must(id) {
    const node = document.getElementById(id);
    if (node === null)
        throw new Error(`页面缺少节点 #${id}`);
    return node;
}
function newConversationId() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
        return crypto.randomUUID();
    }
    return `conv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className !== undefined && className !== "")
        node.className = className;
    if (text !== undefined)
        node.textContent = text;
    return node;
}
async function request(path, body) {
    const headers = { Accept: "application/json" };
    if (session !== "")
        headers[SESSION_HEADER] = session;
    if (body !== undefined)
        headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        credentials: "omit",
    });
    const issued = response.headers.get(SESSION_HEADER);
    if (issued !== null && issued !== "") {
        session = issued;
        sessionStorage.setItem(SESSION_KEY, issued);
    }
    if (!response.ok) {
        const failure = await describeFailure(response);
        throw new ApiFailure(response.status, failure.code, failure.message, failure.uiState);
    }
    return (await response.json());
}
async function describeFailure(response) {
    let body = null;
    try {
        body = await response.json();
    }
    catch {
        body = null;
    }
    const fallback = FALLBACK[response.status] ?? "请求未完成，请稍后重试。";
    if (typeof body !== "object" || body === null) {
        return { code: "", message: fallback, uiState: null };
    }
    const record = body;
    const detail = record["detail"];
    return {
        code: typeof record["code"] === "string" ? record["code"] : "",
        message: typeof detail === "string" && detail !== "" ? detail : fallback,
        uiState: typeof record["ui_state"] === "string" ? record["ui_state"] : null,
    };
}
async function refresh() {
    if (taskId === "") {
        view = null;
        render();
        return;
    }
    try {
        view = await request(`/api/tasks/${taskId}`);
        render();
    }
    catch (error) {
        fail(error);
    }
}
function scheduleRefresh() {
    if (refreshTimer !== null)
        return;
    refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        void refresh();
    }, 120);
}
function connect() {
    closeStream();
    attempts = 0;
    if (taskId === "")
        return;
    void pump();
}
function closeStream() {
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
async function pump() {
    if (taskId === "")
        return;
    const token = ++streamToken;
    const controller = new AbortController();
    streamAbort = controller;
    const headers = { Accept: "text/event-stream" };
    if (session !== "")
        headers[SESSION_HEADER] = session;
    let response;
    try {
        response = await fetch(`/api/tasks/${taskId}/events?after=${lastSequence}`, {
            headers,
            cache: "no-store",
            credentials: "omit",
            signal: controller.signal,
        });
    }
    catch {
        if (token === streamToken)
            reconnect();
        return;
    }
    if (token !== streamToken)
        return;
    if (!response.ok || response.body === null) {
        reconnect();
        return;
    }
    attempts = 0;
    setConnection("live");
    await readStream(response.body, token);
    if (token === streamToken)
        reconnect();
}
async function readStream(body, token) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
        for (;;) {
            const { done, value } = await reader.read();
            if (done || token !== streamToken)
                break;
            buffer = (buffer + decoder.decode(value, { stream: true })).split("\r\n").join("\n");
            let index = buffer.indexOf("\n\n");
            while (index >= 0) {
                consume(buffer.slice(0, index));
                buffer = buffer.slice(index + 2);
                index = buffer.indexOf("\n\n");
            }
        }
    }
    catch {
    }
}
function consume(chunk) {
    for (const line of chunk.split("\n")) {
        if (!line.startsWith("id:"))
            continue;
        const parsed = Number(line.slice(3).trim());
        if (Number.isFinite(parsed) && parsed > lastSequence)
            lastSequence = parsed;
    }
    scheduleRefresh();
}
function reconnect() {
    if (taskId === "")
        return;
    attempts += 1;
    if (attempts >= MAX_ATTEMPTS) {
        setConnection("offline");
        return;
    }
    setConnection("reconnecting");
    const delay = Math.min(1000 * 2 ** (attempts - 1), 8000);
    window.setTimeout(() => void pump(), delay);
}
function setConnection(next) {
    connection = next;
    render();
}
async function perform(kind, field) {
    if (busy)
        return;
    busy = true;
    notice = null;
    const composer = document.getElementById("composer-text");
    if (composer instanceof HTMLTextAreaElement)
        keepText = composer.value;
    render();
    try {
        await dispatch(kind, field);
    }
    catch (error) {
        fail(error);
    }
    finally {
        busy = false;
        render();
    }
}
async function dispatch(kind, field) {
    if (kind === "restart") {
        resetTask();
        return;
    }
    if (kind === "start") {
        await startTask();
        return;
    }
    if (view === null)
        return;
    if (kind === "i_finished") {
        view = await request(`/api/tasks/${view.task_id}/answers`, finishedRequest());
        editing = null;
        keepText = "";
        return;
    }
    if (kind === "answer") {
        if (field === undefined)
            return;
        view = await request(`/api/tasks/${view.task_id}/answers`, answerRequest(field));
        editing = null;
        keepText = "";
        return;
    }
    if (kind === "confirm") {
        view = await request(`/api/tasks/${view.task_id}/confirm`, confirmRequest(view));
        editing = null;
        return;
    }
    if (kind === "cancel") {
        view = await request(`/api/tasks/${view.task_id}/cancel`, {});
        return;
    }
}
async function startTask() {
    const composer = document.getElementById("composer-text");
    const text = composer instanceof HTMLTextAreaElement ? composer.value.trim() : keepText.trim();
    if (text === "") {
        notice = "请先描述您要办理的事情。";
        render();
        return;
    }
    try {
        const created = await request("/api/tasks", {
            conversation_id: conversationId,
            input_text: text,
        });
        view = created;
        taskId = created.task_id;
        sessionStorage.setItem(TASK_KEY, taskId);
        keepText = "";
        connect();
    }
    catch (error) {
        if (error instanceof ApiFailure && error.uiState === "needs_clarification") {
            keepText = text;
            notice = error.message;
            render();
            return;
        }
        throw error;
    }
}
function resetTask() {
    view = null;
    taskId = "";
    editing = null;
    lastSequence = 0;
    keepText = "";
    sessionStorage.removeItem(TASK_KEY);
    closeStream();
    connection = "live";
}
function fail(error) {
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
        if (error.status === 409)
            void refresh();
        return;
    }
    notice = "网络异常，请稍后重试。";
    reconnect();
}
function syncStream(messages) {
    const wanted = new Set(messages.map((message) => message.id));
    for (const node of Array.from(streamEl.children)) {
        const id = node.dataset["msg"] ?? "";
        if (!wanted.has(id))
            node.remove();
    }
    const atBottom = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 24;
    const existing = new Map();
    for (const node of Array.from(streamEl.children)) {
        const element = node;
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
        if (node.textContent !== message.text)
            node.textContent = message.text;
    }
    if (appended || atBottom)
        streamEl.scrollTop = streamEl.scrollHeight;
}
function messageNode(message) {
    const row = el("div", `msg ${message.kind} ${message.role}`);
    row.dataset["msg"] = message.id;
    row.appendChild(el("div", "bubble", message.text));
    return row;
}
function render() {
    renderConnection();
    noticeEl.className = notice === null ? "" : "notice";
    noticeEl.textContent = notice ?? "";
    const screen = screenOf(view);
    if (screen.state !== "awaiting_confirmation")
        editing = null;
    syncStream(transcriptOf(view));
    panelEl.replaceChildren(statusCard(screen));
    if (panelHasContent(screen))
        panelEl.appendChild(panel(screen));
    renderComposer(screen);
}
function renderConnection() {
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
function statusCard(screen) {
    const card = el("section", `card status ${screen.state}`);
    card.dataset["state"] = screen.state;
    card.appendChild(el("h2", "status-title", screen.title));
    card.appendChild(el("p", "hint", screen.hint));
    if (view !== null && view.completed_steps.length > 0 && !screen.terminal) {
        card.appendChild(el("p", "muted", `已完成 ${view.completed_steps.length} 个步骤`));
    }
    return card;
}
function panel(screen) {
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
        box.appendChild(guideBox(screen));
    }
    if (screen.state === "failed" && view !== null && view.failure_code !== null) {
        box.appendChild(el("p", "muted", "如需要进一步说明，请携带受理编号联系办理单位。"));
    }
    if (editing !== null) {
        return box;
    }
    box.appendChild(actionBar(screen));
    return box;
}
function renderComposer(screen) {
    const active = (screen.state === "idle" || screen.state === "needs_clarification") && !busy;
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
                if (send instanceof HTMLButtonElement && !send.disabled)
                    send.click();
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
        composerEl.firstElementChild.textContent = "请描述您要办理的事情";
    }
    else {
        composerEl.firstElementChild.textContent = `现在不用您打字：${screen.state === "awaiting_input" ? "请在页面上方的表单里填写。" : screen.hint}`;
    }
    const text = document.getElementById("composer-text");
    if (text instanceof HTMLTextAreaElement) {
        text.disabled = !active;
        text.placeholder = active
            ? "例如：我想给家里老人办一下高龄补贴"
            : screen.state === "awaiting_input"
                ? "请填写上面的表单"
                : "";
        if (active) {
            if (text.value !== keepText)
                text.value = keepText;
        }
        else if (!busy && text.value !== "") {
            text.value = "";
        }
    }
    const send = document.getElementById("composer-send");
    if (send instanceof HTMLButtonElement)
        send.disabled = !active;
}
function fieldForm(field) {
    const wrapper = el("div", "composer");
    const label = el("label", "", field.label);
    label.setAttribute("for", "field-input");
    const input = el("input");
    input.id = "field-input";
    input.type = "text";
    input.autocomplete = "off";
    input.maxLength = 200;
    input.value = field.value;
    wrapper.appendChild(label);
    wrapper.appendChild(input);
    return wrapper;
}
function editFooter() {
    const row = el("div", "actions");
    const save = actionButton({ kind: "answer", label: "保存修改", primary: true }, () => submitField(editing ?? undefined));
    const back = actionButton({ kind: "edit", label: "不改了", primary: false }, () => {
        editing = null;
        render();
    });
    row.appendChild(save);
    row.appendChild(back);
    return row;
}
function guideBox(screen) {
    const list = el("dl", "guide");
    const guide = screen.guide;
    if (guide === null)
        return list;
    const add = (term, value) => {
        list.appendChild(el("dt", "", term));
        list.appendChild(el("dd", "", value));
    };
    add("要做什么", guide.what);
    add("去哪里", guide.where);
    add("有效期", guide.expires);
    return list;
}
function summaryList(screen, editable) {
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
function actionBar(screen) {
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
function onClick(action, screen) {
    if (action.kind === "i_finished") {
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
function submitField(name) {
    if (name === undefined || view === null)
        return;
    const input = document.getElementById("field-input");
    const value = input instanceof HTMLInputElement ? input.value : "";
    const field = { name, label: fieldOf(view, name).label, value };
    if (!canSubmit(field)) {
        notice = "请先填写后再提交。";
        render();
        return;
    }
    void perform("answer", field);
}
function actionButton(action, handler, disabled = false) {
    const button = el("button", action.primary ? "primary" : "", action.label);
    button.type = "button";
    button.disabled = disabled || busy;
    button.addEventListener("click", handler);
    return button;
}
async function boot() {
    render();
    if (taskId === "")
        return;
    try {
        view = await request(`/api/tasks/${taskId}`);
    }
    catch (error) {
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
