export const LOCAL_MIN_POSITIVE = 3;
export const LOCAL_MIN_NEAR_MISS = 2;
export const LOCAL_ADVISED_OUT_OF_SCOPE = 1;
export const SAMPLE_ID_MAX = 128;
export const SAMPLE_TEXT_MAX = 500;
export const SAMPLE_CATEGORIES = [
    "positive",
    "near_miss",
    "out_of_scope",
];
export const CATEGORY_LABEL = {
    positive: "正例",
    near_miss: "近似误例",
    out_of_scope: "超范围例",
};
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
export function checkSamples(rows) {
    const counts = {
        positive: 0,
        near_miss: 0,
        out_of_scope: 0,
    };
    const problems = [];
    const seenIds = new Set();
    const seenTexts = new Set();
    rows.forEach((row, index) => {
        const id = row.sample_id.trim();
        const text = row.text.trim();
        const at = (field) => `intent_samples.${index}.${field}`;
        const categoryOk = SAMPLE_CATEGORIES.includes(row.category);
        const before = problems.length;
        if (!categoryOk) {
            problems.push({ loc: at("category"), message: "类别必须是三类样例之一。" });
        }
        if (id === "" && text === "" && categoryOk) {
            return;
        }
        if (id === "") {
            problems.push({ loc: at("sample_id"), message: "sample_id 不能为空。" });
        }
        else if (id.length > SAMPLE_ID_MAX) {
            problems.push({ loc: at("sample_id"), message: `sample_id 不能超过 ${SAMPLE_ID_MAX} 字。` });
        }
        else if (seenIds.has(id)) {
            problems.push({ loc: at("sample_id"), message: `sample_id「${id}」重复。` });
        }
        else {
            seenIds.add(id);
        }
        if (text === "") {
            problems.push({ loc: at("text"), message: "话术不能为空。" });
        }
        else if (text.length > SAMPLE_TEXT_MAX) {
            problems.push({ loc: at("text"), message: `话术不能超过 ${SAMPLE_TEXT_MAX} 字。` });
        }
        else if (seenTexts.has(text)) {
            problems.push({ loc: at("text"), message: "话术与前面某条完全相同，请换一种说法。" });
        }
        else {
            seenTexts.add(text);
        }
        if (categoryOk && problems.length === before) {
            counts[row.category] += 1;
        }
    });
    const shortfalls = [];
    if (counts.positive < LOCAL_MIN_POSITIVE) {
        shortfalls.push(`正例不足：${counts.positive}/${LOCAL_MIN_POSITIVE}，还缺 ` +
            `${LOCAL_MIN_POSITIVE - counts.positive} 条`);
    }
    if (counts.near_miss < LOCAL_MIN_NEAR_MISS) {
        shortfalls.push(`近似误例不足：${counts.near_miss}/${LOCAL_MIN_NEAR_MISS}，还缺 ` +
            `${LOCAL_MIN_NEAR_MISS - counts.near_miss} 条`);
    }
    const advisory = [];
    if (counts.out_of_scope < LOCAL_ADVISED_OUT_OF_SCOPE) {
        advisory.push("建议至少补 1 条超范围例（服务端不强制，但能挡住寒暄被误路由）。");
    }
    return {
        ready: problems.length === 0 && shortfalls.length === 0,
        counts,
        shortfalls,
        problems,
        advisory,
    };
}
export function sampleGateDecision(check) {
    const disabled = !check.ready;
    const title = check.ready
        ? `本地预检通过：正例 ${check.counts.positive} 条、近似误例 ` +
            `${check.counts.near_miss} 条、超范围例 ${check.counts.out_of_scope} 条。`
        : "本地预检未通过，「保存草稿」已禁用：";
    const items = [
        ...check.shortfalls.map((item) => `<li>缺：${escapeHtml(item)}</li>`),
        ...check.problems.map((item) => `<li>${escapeHtml(item.loc)}：${escapeHtml(item.message)}</li>`),
        ...check.advisory.map((item) => `<li>${escapeHtml(item)}</li>`),
    ];
    const list = items.length > 0 ? `<ul>${items.join("")}</ul>` : "";
    const authority = '<div style="margin-top:6px">本地条数只是提示，最终以服务端发布门禁的 409 报告为准。</div>';
    return {
        disabled,
        className: check.ready ? "ok" : "warn",
        html: `<strong>${escapeHtml(title)}</strong>${list}${authority}`,
    };
}
