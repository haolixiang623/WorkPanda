import { checkSamples, sampleGateDecision, SAMPLE_CATEGORIES, } from "./sample_check.js";
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
function normalizeErrorPayload(payload) {
    if (typeof payload !== "object" || payload === null) {
        return {};
    }
    const raw = payload;
    const nested = raw.detail;
    if (typeof nested === "object" && nested !== null && !Array.isArray(nested)) {
        return nested;
    }
    return raw;
}
function isReplayReport(report) {
    return report !== undefined && Array.isArray(report.results);
}
function intentGateDetails(payload) {
    if (isReplayReport(payload.report) || payload.report === undefined) {
        return [];
    }
    const items = [];
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
function renderIntentGate(payload) {
    const report = payload.report;
    const items = intentGateDetails(payload);
    if (items.length === 0 || report === undefined || isReplayReport(report)) {
        return;
    }
    const box = el("sample-status");
    box.className = "warn";
    box.innerHTML =
        `<strong>服务端发布门禁拦下（权威结论，状态 ${escapeHtml(report.status ?? "fail")}）</strong>` +
            `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` +
            "<div>界面本地条数只是提示；请按这里的缺口补齐后重新发布。</div>";
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
        const entries = [
            ...problems.map((item) => `${item.loc.join(".")}：${item.message}`),
            ...intentGateDetails(payload),
        ];
        const list = entries.length > 0
            ? `<ul>${entries.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
            : "";
        showMessage("err", `<strong>[${error.status}] ${escapeHtml(payload.code ?? "request_failed")}</strong> ` +
            `${escapeHtml(detailText(payload))}${list}`);
        renderIntentGate(payload);
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
        throw new ApiFailure(response.status, normalizeErrorPayload(payload));
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
const SAMPLE_TEMPLATE = `
  <div class="field"><label>sample_id</label><input data-key="sample_id" data-loc="intent_samples.IDX.sample_id" /></div>
  <div class="field"><label>类别</label><select data-key="category" data-loc="intent_samples.IDX.category">
    <option value="positive">正例</option><option value="near_miss">近似误例</option>
    <option value="out_of_scope">超范围例</option></select></div>
  <div class="field"><label>话术（合成 / 脱敏）</label><input data-key="text" data-loc="intent_samples.IDX.text" /></div>
  <div class="field"><label></label><button type="button" data-remove="1" class="danger">删除</button></div>
`;
function renumberSamples() {
    document.querySelectorAll("#samples .sample").forEach((row, index) => {
        row.querySelectorAll("[data-loc]").forEach((input) => {
            const key = input.dataset.loc?.split(".").pop() ?? "";
            input.dataset.loc = `intent_samples.${index}.${key}`;
        });
    });
}
function addSampleRow(category, initial) {
    const row = document.createElement("div");
    row.className = "sample";
    row.innerHTML = SAMPLE_TEMPLATE.replace(/IDX/g, "__NEW__");
    const index = el("samples").children.length;
    row.querySelectorAll("[data-loc]").forEach((input) => {
        const key = input.dataset.loc?.split(".").pop() ?? "";
        input.dataset.loc = `intent_samples.${index}.${key}`;
    });
    const categoryInput = row.querySelector('[data-key="category"]');
    if (categoryInput !== null) {
        categoryInput.value = category;
    }
    if (initial !== undefined) {
        for (const [key, raw] of Object.entries(initial)) {
            const input = row.querySelector(`[data-key="${key}"]`);
            if (input !== null && raw !== null && raw !== undefined) {
                input.value = String(raw);
            }
        }
    }
    row.querySelector("[data-remove]")?.addEventListener("click", () => {
        row.remove();
        renumberSamples();
        refreshSampleGate();
    });
    el("samples").append(row);
}
function readSample(row) {
    const read = (key) => {
        const input = row.querySelector(`[data-key="${key}"]`);
        return input === null ? "" : input.value.trim();
    };
    return {
        sample_id: read("sample_id"),
        category: read("category"),
        text: read("text"),
    };
}
function refreshSampleGate() {
    const samples = Array.from(document.querySelectorAll("#samples .sample")).map(readSample);
    const decision = sampleGateDecision(checkSamples(samples));
    const box = el("sample-status");
    box.className = decision.className;
    box.innerHTML = decision.html;
    el("save-draft").disabled = decision.disabled;
}
function resetSamples() {
    el("samples").replaceChildren();
    for (const category of SAMPLE_CATEGORIES) {
        const need = category === "positive" ? 3 : category === "near_miss" ? 2 : 1;
        for (let index = 0; index < need; index += 1) {
            addSampleRow(category);
        }
    }
    refreshSampleGate();
}
function loadSamples(samples) {
    el("samples").replaceChildren();
    for (const sample of samples) {
        if (!SAMPLE_CATEGORIES.includes(sample.category)) {
            continue;
        }
        addSampleRow(sample.category, sample);
    }
    refreshSampleGate();
}
function readDraft() {
    return {
        flow_id: value("draft-flow"),
        version: value("draft-version"),
        title: value("draft-title"),
        steps: Array.from(document.querySelectorAll("#steps .step")).map(readStep),
        intent_samples: Array.from(document.querySelectorAll("#samples .sample")).map(readSample),
    };
}
async function saveDraft() {
    clearInvalid();
    const payload = readDraft();
    const gate = checkSamples(payload.intent_samples);
    if (!gate.ready) {
        refreshSampleGate();
        const reasons = [
            ...gate.shortfalls,
            ...gate.problems.map((problem) => `${problem.loc}：${problem.message}`),
        ];
        showMessage("err", `本地预检未通过，草稿未提交：${escapeHtml(reasons.join("；"))}`);
        return;
    }
    const created = await api("/api/admin/flows", "POST", payload);
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
    loadSamples(detail.flow.intent_samples ?? []);
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
            `${escapeHtml(outcome.message)}；回放门禁${outcome.replay_ran ? "已执行" : "未执行（该版本没有用例）"}。`);
        await refreshFlows();
    }
    catch (error) {
        if (error instanceof ApiFailure && isReplayReport(error.payload.report)) {
            renderReport(error.payload.report);
        }
        throw error;
    }
}
async function rollback(flow) {
    await api(`/api/admin/flows/${encodeURIComponent(flow.flow_id)}/rollback`, "POST", { version: flow.version });
    showMessage("ok", `已回滚 ${escapeHtml(flow.flow_id)} 到 ${escapeHtml(flow.version)}；在途任务仍按各自版本继续。`);
    await refreshFlows();
}
async function disable() {
    const flowId = value("action-flow");
    if (flowId === "") {
        throw new Error("请先填写 flow_id。");
    }
    await api(`/api/admin/flows/${encodeURIComponent(flowId)}/disable`, "POST");
    showMessage("ok", `${escapeHtml(flowId)} 已停用（历史版本保留，可随时回滚）。`);
    await refreshFlows();
}
function bind(id, action) {
    el(id).addEventListener("click", () => void guarded(action));
}
el("api-base").value = location.origin;
addStepRow({ step_id: "collect", kind: "collect", title: "收集信息" });
addStepRow({ step_id: "submit", kind: "execute", title: "提交", tool: "example.tool" });
resetSamples();
const samplesBox = el("samples");
samplesBox.addEventListener("input", refreshSampleGate);
samplesBox.addEventListener("change", refreshSampleGate);
for (const [id, category] of [
    ["add-sample-positive", "positive"],
    ["add-sample-near-miss", "near_miss"],
    ["add-sample-out-of-scope", "out_of_scope"],
]) {
    el(id).addEventListener("click", () => {
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
    resetSamples();
    clearInvalid();
    showMessage("ok", "表单已清空（含意图样例）。");
});
