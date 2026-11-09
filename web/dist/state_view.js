export const UI_STATES = [
    "idle",
    "received",
    "processing",
    "needs_clarification",
    "awaiting_input",
    "awaiting_action",
    "awaiting_confirmation",
    "completed",
    "failed",
    "outcome_unconfirmed",
    "handoff",
    "cancelled",
];
const START = { kind: "start", label: "开始办理", primary: true };
const SUBMIT = { kind: "answer", label: "提交", primary: true };
const CONFIRM = { kind: "confirm", label: "确认提交", primary: true };
const EDIT = { kind: "edit", label: "返回修改", primary: false };
const CANCEL = { kind: "cancel", label: "取消本次办理", primary: false };
const RESTART = { kind: "restart", label: "重新发起", primary: true };
const I_FINISHED = { kind: "i_finished", label: "我已完成", primary: true };
function emptyScreen(state, title, hint) {
    return {
        state,
        title,
        hint,
        field: null,
        summary: [],
        impact: null,
        guide: null,
        actions: [],
        blockedReason: null,
        terminal: false,
    };
}
export function idleScreen() {
    return {
        ...emptyScreen("idle", "可以开始", "请描述您要办理的事情，说得口语一点也可以。"),
        actions: [START],
    };
}
function labelOf(view, name) {
    const label = view.field_labels[name];
    return label === undefined || label === "" ? name : label;
}
export function collected(view) {
    const rows = [];
    for (const [name, value] of Object.entries(view.slots)) {
        if (value === "")
            continue;
        rows.push({ name, label: labelOf(view, name), value });
    }
    return rows;
}
export function resultRows(result) {
    if (result === null)
        return [];
    const rows = [];
    for (const [name, value] of Object.entries(result)) {
        if (name.startsWith("_"))
            continue;
        const kind = typeof value;
        if (kind !== "string" && kind !== "number" && kind !== "boolean")
            continue;
        if (value === "")
            continue;
        rows.push({ name, label: name, value: String(value) });
    }
    return rows;
}
export function nextField(view) {
    const name = view.pending_fields[0];
    if (name === undefined)
        return null;
    return { name, label: labelOf(view, name), value: view.slots[name] ?? "" };
}
export function fieldOf(view, name) {
    return { name, label: labelOf(view, name), value: view.slots[name] ?? "" };
}
export function canSubmit(field) {
    return field !== null && field.value.trim().length > 0;
}
export function screenStateOf(view) {
    const declared = UI_STATES.find((state) => state === view.ui_state);
    return declared ?? "outcome_unconfirmed";
}
export function screenOf(view) {
    if (view === null)
        return idleScreen();
    const state = screenStateOf(view);
    const base = emptyScreen(state, view.ui_title, view.ui_hint);
    switch (state) {
        case "received":
        case "processing":
            return { ...base, actions: [CANCEL] };
        case "needs_clarification":
            return { ...base, actions: [{ kind: "start", label: "重新描述", primary: true }] };
        case "awaiting_input": {
            const field = nextField(view);
            const summary = collected(view);
            return {
                ...base,
                hint: view.current_step_prompt ?? base.hint,
                field,
                summary,
                actions: field === null ? [CANCEL] : [SUBMIT, CANCEL],
            };
        }
        case "awaiting_confirmation": {
            const summary = collected(view);
            const actions = [CONFIRM];
            if (summary.length > 0)
                actions.push(EDIT);
            actions.push(CANCEL);
            return { ...base, summary, impact: view.confirm_message, actions };
        }
        case "awaiting_action": {
            const guide = view.user_action;
            const expired = guide !== null && guide.remaining_seconds <= 0;
            const checking = guide !== null && guide.awaiting_check;
            const actions = [];
            if (!expired && !checking)
                actions.push(I_FINISHED);
            actions.push(CANCEL);
            return {
                ...base,
                field: null,
                summary: collected(view),
                guide: {
                    what: guide === null ? (view.current_step_title ?? "按页面提示完成操作") : guide.what,
                    where: guide === null ? "请按办理页面上的提示操作" : guide.where,
                    expires: expiresText(guide),
                },
                actions,
                blockedReason: expired
                    ? "这次操作已经超过有效期，需要转人工协助；请不要重复尝试。"
                    : checking
                        ? "正在核实您刚才的操作，请稍候。"
                        : null,
            };
        }
        case "completed":
            return { ...base, summary: resultRows(view.result), actions: [RESTART], terminal: true };
        case "failed":
            return { ...base, actions: [RESTART], terminal: true };
        case "outcome_unconfirmed":
            return { ...base, terminal: true };
        case "handoff":
            return { ...base, terminal: true };
        case "cancelled":
            return { ...base, actions: [RESTART], terminal: true };
        case "idle":
            return { ...base, actions: [START] };
    }
}
function expiresText(guide) {
    if (guide === null)
        return "有效期以办理页面上的提示为准";
    if (guide.remaining_seconds <= 0)
        return "已超过有效期";
    const minutes = Math.floor(guide.remaining_seconds / 60);
    if (minutes < 1)
        return "请在接下来的一分钟内完成";
    return `请在接下来的 ${minutes} 分钟内完成`;
}
export function finishedRequest() {
    return { values: { [I_FINISHED_KEY]: I_FINISHED_VALUE } };
}
const I_FINISHED_KEY = "i_finished";
const I_FINISHED_VALUE = "done";
export function answerRequest(field) {
    return { values: { [field.name]: field.value } };
}
export function confirmRequest(view) {
    if (view.current_step === null) {
        throw new Error("当前没有可确认的步骤");
    }
    return { step_id: view.current_step, accepted: true };
}
export function connectionNotice(connection) {
    if (connection === "live")
        return null;
    if (connection === "reconnecting") {
        return { level: "reconnecting", text: "连接中断，正在重新连接；任务可能仍在继续。" };
    }
    return { level: "offline", text: "无法连接服务端，请检查网络后重试。" };
}
export function maskValue(value) {
    const chars = [...value];
    if (chars.length <= 4)
        return "*".repeat(chars.length);
    const tail = 4;
    const head = chars.length >= 12 ? 6 : 0;
    return (chars.slice(0, head).join("") +
        "*".repeat(chars.length - head - tail) +
        chars.slice(-tail).join(""));
}
function isSensitive(view, name) {
    return view.sensitive_fields.indexOf(name) >= 0;
}
export function displayValue(view, name) {
    const value = view.slots[name] ?? "";
    if (value === "")
        return "";
    return isSensitive(view, name) ? maskValue(value) : value;
}
function stepOf(view) {
    return view.current_step ?? "-";
}
export function transcriptOf(view) {
    const messages = [];
    if (view === null) {
        messages.push({
            id: "opening:idle",
            kind: "opening",
            role: "assistant",
            text: "请用一句话说您要办什么，我再一步步问清楚。",
        });
        return messages;
    }
    const state = screenStateOf(view);
    if (view.flow_title !== null && view.flow_title !== "") {
        messages.push({
            id: "opening:flow",
            kind: "opening",
            role: "assistant",
            text: `您要办的是：${view.flow_title}。`,
        });
    }
    if (state === "needs_clarification") {
        messages.push({
            id: "clarify:ask",
            kind: "assistant",
            role: "assistant",
            text: view.ui_hint,
        });
        return messages;
    }
    for (const name of Object.keys(view.slots)) {
        const shown = displayValue(view, name);
        if (shown === "")
            continue;
        const label = labelOf(view, name);
        messages.push({
            id: `q:${stepOf(view)}:${name}`,
            kind: "field_question",
            role: "assistant",
            text: label,
        });
        messages.push({
            id: `a:${stepOf(view)}:${name}`,
            kind: "user_answer",
            role: "user",
            text: shown,
        });
    }
    if (state === "awaiting_input" ||
        state === "awaiting_action" ||
        state === "received" ||
        state === "processing") {
        const field = nextField(view);
        if (field !== null && state === "awaiting_input") {
            if (view.current_step_prompt !== null && view.current_step_prompt !== "") {
                messages.push({
                    id: `prompt:${stepOf(view)}`,
                    kind: "assistant",
                    role: "assistant",
                    text: view.current_step_prompt,
                });
            }
            messages.push({
                id: `q:${stepOf(view)}:${field.name}:open`,
                kind: "field_question",
                role: "assistant",
                text: field.label,
            });
        }
        else if (state === "awaiting_action") {
            const guide = view.user_action;
            messages.push({
                id: `guide:${stepOf(view)}:what`,
                kind: "guide",
                role: "assistant",
                text: `这一步需要您本人操作：${guide === null ? "请按页面提示完成操作" : guide.what}。`,
            });
            messages.push({
                id: `guide:${stepOf(view)}:where`,
                kind: "guide",
                role: "assistant",
                text: guide === null ? "请按办理页面上的提示操作。" : `在哪里做：${guide.where}。`,
            });
            messages.push({
                id: `guide:${stepOf(view)}:expires`,
                kind: "guide",
                role: "assistant",
                text: guide === null ? "有效期以办理页面上的提示为准。" : `${expiresText(guide)}。`,
            });
        }
        else if (state === "received" || state === "processing") {
            messages.push({
                id: `status:${state}`,
                kind: "status",
                role: "assistant",
                text: view.ui_hint,
            });
        }
    }
    if (state === "awaiting_confirmation") {
        const rows = collected(view).map((row) => `${row.label}：${displayValue(view, row.name)}`);
        messages.push({
            id: "confirm:summary",
            kind: "confirmation",
            role: "assistant",
            text: rows.length === 0
                ? "请确认是否提交。"
                : `请核对以下内容：${rows.join("；")}。`,
        });
        if (view.current_step_title !== null && view.current_step_title !== "") {
            messages.push({
                id: "confirm:action",
                kind: "confirmation",
                role: "assistant",
                text: `将要执行：${view.current_step_title}。`,
            });
        }
        if (view.confirm_message !== null && view.confirm_message !== "") {
            messages.push({
                id: "confirm:impact",
                kind: "confirmation",
                role: "assistant",
                text: view.confirm_message,
            });
        }
    }
    if (state === "completed") {
        for (const row of resultRows(view.result)) {
            messages.push({
                id: `result:${row.name}`,
                kind: "result",
                role: "assistant",
                text: `${row.label}：${row.value}`,
            });
        }
        messages.push({
            id: "status:completed",
            kind: "status",
            role: "assistant",
            text: view.ui_title,
        });
        return messages;
    }
    if (state === "failed" ||
        state === "outcome_unconfirmed" ||
        state === "handoff" ||
        state === "cancelled") {
        messages.push({
            id: `status:${state}`,
            kind: "status",
            role: "assistant",
            text: view.ui_title,
        });
    }
    return messages;
}
