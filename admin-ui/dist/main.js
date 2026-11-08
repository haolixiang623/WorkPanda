"use strict";
class ApiFailure extends Error {
    constructor(status, payload) {
        super(`HTTP ${status}`);
        this.status = status;
        this.payload = payload;
    }
}
function el(id) {
    const found = document.getElementById(id);
    if (found === null) {
        throw new Error(`页面缺少元素 #${id}`);
    }
    return found;
}
function value(id) {
    return el(id).value.trim();
}
function escapeHtml(raw) {
    return raw.replace(/[&<>"']/g, (char) => {
        const table = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        };
        return table[char] ?? char;
    });
}
/** 结果消息里的署名片段；服务端没给就不显示，绝不本地编一个主体名。 */
function actorSuffix(actor, label) {
    return actor ? `；${label}：<strong>${escapeHtml(actor)}</strong>` : "";
}
function showMessage(kind, html) {
    const box = el("message");
    box.className = kind;
    box.innerHTML = html;
}
function clearInvalid() {
    document.querySelectorAll(".invalid").forEach((node) => node.classList.remove("invalid"));
}
function markInvalid(fields) {
    clearInvalid();
    for (const field of fields) {
        const key = field.loc.filter((part) => part !== "body").join(".");
        document.querySelectorAll(`[data-loc="${key}"]`).forEach((node) => {
            node.classList.add("invalid");
        });
    }
}
function detailText(payload) {
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
function showFailure(error) {
    if (error instanceof ApiFailure) {
        const payload = error.payload;
        const problems = payload.fields ??
            (Array.isArray(payload.detail)
                ? payload.detail.map((issue) => ({
                    loc: issue.loc.filter((part) => part !== "body"),
                    message: issue.msg,
                }))
                : []);
        if (problems.length > 0) {
            markInvalid(problems);
        }
        const list = problems.length > 0
            ? `<ul>${problems
                .map((item) => `<li>${escapeHtml(item.loc.join("."))}：${escapeHtml(item.message)}</li>`)
                .join("")}</ul>`
            : "";
        showMessage("err", `<strong>[${error.status}] ${escapeHtml(payload.code ?? "request_failed")}</strong> ` +
            `${escapeHtml(detailText(payload))}${list}`);
    }
    else {
        showMessage("err", escapeHtml(String(error)));
    }
}
async function api(path, method = "GET", body) {
    const token = value("admin-token");
    const base = (value("api-base") || location.origin).replace(/\/+$/, "");
    const headers = { "Content-Type": "application/json" };
    if (token !== "") {
        headers["X-WorkPanda-Admin-Token"] = token;
    }
    const response = await fetch(base + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    if (text !== "") {
        try {
            payload = JSON.parse(text);
        }
        catch {
            payload = { detail: text };
        }
    }
    if (!response.ok) {
        throw new ApiFailure(response.status, (payload ?? {}));
    }
    return payload;
}
async function guarded(action) {
    try {
        await action();
    }
    catch (error) {
        showFailure(error);
    }
}
function actionButton(label, onClick, extra) {
    const button = document.createElement("button");
    button.textContent = label;
    if (extra !== undefined) {
        button.className = extra;
    }
    button.addEventListener("click", () => void guarded(onClick));
    return button;
}
function renderFlows(flows) {
    const tbody = el("flow-list");
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
        if (flow.published) {
            const tag = document.createElement("span");
            tag.className = "tag on";
            tag.textContent = "已发布";
            stateCell.replaceChildren(tag);
        }
        else {
            const tag = document.createElement("span");
            tag.className = "tag off";
            tag.textContent = "草稿";
            stateCell.replaceChildren(tag);
        }
        // 责任锚点：已发布显示发布人，停用后显示最近停用人；服务端没有署名时留空。
        const actorNote = flow.published
            ? (flow.published_by ? `发布人：${flow.published_by}` : "")
            : (flow.disabled_by ? `最近停用：${flow.disabled_by}` : "");
        if (actorNote !== "") {
            const note = document.createElement("div");
            note.className = "muted";
            note.textContent = actorNote;
            stateCell.append(note);
        }
        row.insertCell().textContent = String(flow.case_count);
        const actions = row.insertCell();
        const container = document.createElement("div");
        container.className = "actions";
        container.append(actionButton("载入编辑", () => loadIntoDraft(flow)), actionButton("查看用例", () => loadCases(flow)), actionButton("回放", () => runReplay(flow)), actionButton("发布", () => publish(flow, true), "primary"), actionButton("回滚到此版本", () => rollback(flow), "danger"));
        actions.append(container);
    }
}
async function refreshFlows() {
    const flows = await api("/api/admin/flows");
    renderFlows(flows);
    showMessage("ok", `已读取 ${flows.length} 个版本。`);
}
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
function renumberSteps() {
    document.querySelectorAll("#steps .step").forEach((row, index) => {
        row.querySelectorAll("[data-loc]").forEach((input) => {
            const key = input.dataset.loc?.split(".").pop() ?? "";
            input.dataset.loc = `steps.${index}.${key}`;
        });
    });
}
function addStepRow(initial) {
    const row = document.createElement("div");
    row.className = "step";
    row.innerHTML = STEP_TEMPLATE.replace(/IDX/g, "__NEW__");
    const index = el("steps").children.length;
    row.querySelectorAll("[data-loc]").forEach((input) => {
        const key = input.dataset.loc?.split(".").pop() ?? "";
        input.dataset.loc = `steps.${index}.${key}`;
    });
    if (initial !== undefined) {
        for (const [key, raw] of Object.entries(initial)) {
            const input = row.querySelector(`[data-key="${key}"]`);
            if (input === null) {
                continue;
            }
            if (input instanceof HTMLInputElement && input.type === "checkbox") {
                input.checked = Boolean(raw);
            }
            else if (Array.isArray(raw)) {
                input.value = raw.join(",");
            }
            else if (raw !== null && raw !== undefined) {
                input.value = String(raw);
            }
        }
    }
    row.querySelector("[data-remove]")?.addEventListener("click", () => {
        row.remove();
        renumberSteps();
    });
    el("steps").append(row);
}
function readStep(row) {
    const read = (key) => {
        const input = row.querySelector(`[data-key="${key}"]`);
        return input === null ? "" : input.value.trim();
    };
    const optional = (key) => {
        const text = read(key);
        return text === "" ? undefined : text;
    };
    const checkbox = row.querySelector('[data-key="requires_confirmation"]');
    return {
        step_id: read("step_id"),
        kind: read("kind"),
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
function readDraft() {
    return {
        flow_id: value("draft-flow"),
        version: value("draft-version"),
        title: value("draft-title"),
        steps: Array.from(document.querySelectorAll("#steps .step")).map(readStep),
    };
}
async function saveDraft() {
    clearInvalid();
    const created = await api("/api/admin/flows", "POST", readDraft());
    showMessage("ok", `草稿 ${escapeHtml(created.flow_id)}@${escapeHtml(created.version)} 已保存（未发布）。`);
    await refreshFlows();
}
async function loadIntoDraft(flow) {
    const detail = await api(`/api/admin/flows/${encodeURIComponent(flow.flow_id)}/versions/${encodeURIComponent(flow.version)}`);
    el("draft-flow").value = detail.flow.flow_id;
    el("draft-version").value = "";
    el("draft-title").value = detail.flow.title;
    const steps = el("steps");
    steps.replaceChildren();
    for (const step of detail.flow.steps) {
        addStepRow(step);
    }
    showMessage("ok", `已载入 ${escapeHtml(flow.flow_id)}@${escapeHtml(flow.version)}；另存请填写新版本号。`);
}
function versionPath() {
    const flowId = value("case-flow");
    const version = value("case-version");
    if (flowId === "" || version === "") {
        throw new Error("请先填写 flow_id 与 version。");
    }
    return `/api/admin/flows/${encodeURIComponent(flowId)}/versions/${encodeURIComponent(version)}`;
}
function splitList(raw) {
    return raw.split(/[,，]/).map((item) => item.trim()).filter((item) => item !== "");
}
async function addCase() {
    let slots = {};
    const rawSlots = value("case-slots");
    if (rawSlots !== "") {
        const parsed = JSON.parse(rawSlots);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new Error("slots 必须是 JSON 对象。");
        }
        slots = parsed;
    }
    const steps = splitList(value("case-steps"));
    const tools = splitList(value("case-tools"));
    const terminal = value("case-terminal");
    const expect = {};
    if (value("case-steps") !== "") {
        expect.steps = steps;
    }
    if (value("case-tools") !== "") {
        expect.tools = tools;
    }
    if (terminal !== "") {
        expect.terminal = terminal;
    }
    const created = await api(`${versionPath()}/cases`, "POST", {
        case_id: value("case-id"),
        slots,
        expect,
    });
    showMessage("ok", `用例 ${escapeHtml(created.case_id)} 已保存。`);
    await loadCases({ flow_id: value("case-flow"), version: value("case-version") });
}
function renderCases(cases, path) {
    const box = el("cases");
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
        cell.append(actionButton("删除", async () => {
            await api(`${path}/cases/${encodeURIComponent(item.case_id)}`, "DELETE");
            showMessage("ok", `用例 ${escapeHtml(item.case_id)} 已删除。`);
            await loadCases({ flow_id: value("case-flow"), version: value("case-version") });
        }, "danger"));
    }
    box.append(table);
}
async function loadCases(flow) {
    el("case-flow").value = flow.flow_id;
    el("case-version").value = flow.version;
    const detail = await api(versionPath());
    renderCases(detail.cases, versionPath());
    showMessage("ok", `${escapeHtml(flow.flow_id)}@${escapeHtml(flow.version)}：${detail.cases.length} 条用例；` +
        `当前${detail.published ? "已发布" : "未发布"}。`);
}
function renderReport(report) {
    const box = el("replay-result");
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
async function runReplay(flow) {
    el("case-flow").value = flow.flow_id;
    el("case-version").value = flow.version;
    const report = await api(`${versionPath()}/replay`, "POST");
    renderReport(report);
    showMessage("ok", `回放完成，总判定 ${escapeHtml(report.verdict)}。`);
}
function actionTarget() {
    const flowId = value("action-flow");
    const version = value("action-version");
    if (flowId === "" || version === "") {
        throw new Error("请先填写 flow_id 与 version。");
    }
    return { flow_id: flowId, version };
}
async function publish(flow, apply) {
    el("action-flow").value = flow.flow_id;
    el("action-version").value = flow.version;
    try {
        const outcome = await api(`/api/admin/flows/${encodeURIComponent(flow.flow_id)}/publish`, "POST", { version: flow.version, apply });
        showMessage("ok", `<strong>${outcome.published ? "已发布" : "预演通过，未发布"}</strong>：` +
            `${escapeHtml(outcome.message)}；回放门禁${outcome.replay_ran ? "已执行" : "未执行（该版本没有用例）"}` +
            `${outcome.published ? actorSuffix(outcome.published_by, "发布人") : ""}。`);
        await refreshFlows();
    }
    catch (error) {
        if (error instanceof ApiFailure && error.payload.report !== undefined) {
            renderReport(error.payload.report);
        }
        throw error;
    }
}
async function rollback(flow) {
    const result = await api(`/api/admin/flows/${encodeURIComponent(flow.flow_id)}/rollback`, "POST", { version: flow.version });
    showMessage("ok", `已回滚 ${escapeHtml(flow.flow_id)} 到 ${escapeHtml(flow.version)}；在途任务仍按各自版本继续` +
        `${actorSuffix(result.published_by, "回滚操作人")}。`);
    await refreshFlows();
}
async function disable() {
    const flowId = value("action-flow");
    if (flowId === "") {
        throw new Error("请先填写 flow_id。");
    }
    const result = await api(`/api/admin/flows/${encodeURIComponent(flowId)}/disable`, "POST");
    showMessage("ok", `${escapeHtml(flowId)} 已停用（历史版本保留，可随时回滚）` +
        `${actorSuffix(result.disabled_by, "停用操作人")}。`);
    await refreshFlows();
}
function bind(id, action) {
    el(id).addEventListener("click", () => void guarded(action));
}
el("api-base").value = location.origin;
addStepRow({ step_id: "collect", kind: "collect", title: "收集信息" });
addStepRow({ step_id: "submit", kind: "execute", title: "提交", tool: "example.tool" });
bind("refresh", refreshFlows);
bind("save-draft", saveDraft);
bind("add-case", addCase);
bind("load-cases", () => loadCases(actionTargetForCases()));
bind("run-replay", () => runReplay(actionTargetForCases()));
bind("dry-run", () => publish(actionTarget(), false));
bind("do-publish", () => publish(actionTarget(), true));
bind("do-rollback", () => rollback(actionTarget()));
bind("do-disable", disable);
function actionTargetForCases() {
    return { flow_id: value("case-flow"), version: value("case-version") };
}
el("add-step").addEventListener("click", () => {
    addStepRow();
});
el("clear-draft").addEventListener("click", () => {
    for (const id of ["draft-flow", "draft-version", "draft-title"]) {
        el(id).value = "";
    }
    el("steps").replaceChildren();
    addStepRow();
    clearInvalid();
    showMessage("ok", "表单已清空。");
});
