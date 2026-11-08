export const UI_STATES = [
    "idle",
    "received",
    "processing",
    "needs_clarification",
    "awaiting_input",
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
    if (declared === undefined)
        return "outcome_unconfirmed";
    if (declared === "awaiting_input" && view.pending_fields.length === 0) {
        return "user_action_required";
    }
    return declared;
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
        case "user_action_required": {
            const field = nextField(view);
            return {
                ...base,
                title: "需要您本人操作",
                hint: "请在办理页面完成操作，完成后回到这里继续。",
                field,
                guide: {
                    what: view.current_step_title ?? "按页面提示完成操作",
                    where: view.current_step_prompt ?? "请按办理页面上的提示操作",
                    expires: "有效期以办理页面上的提示为准",
                },
                actions: [I_FINISHED, CANCEL],
                blockedReason: field === null
                    ? "当前没有需要提交的内容；在办理页面完成后，本页会自动更新。"
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
