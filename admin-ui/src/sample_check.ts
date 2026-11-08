/**
 * 管理侧「意图样例」本地预校验（Issue #62）。
 *
 * 刻意做成不碰 DOM、不发网络请求的纯模块：
 * - 浏览器里由 main.ts 调用，在保存草稿之前就把「缺哪一类、缺几条」显示出来；
 * - 回归里可以直接 import 编译产物，验证按钮禁用与提示文案，不必起浏览器。
 *
 * 这里的条数下限只是**本地提示**，不是发布规则。服务端 `IntentReleaseGate` 用
 * `Settings.intent_eval_min_positive` / `intent_eval_min_near_miss` 独立裁决；
 * 两边一旦漂移，以服务端 409 报告为准。所以本模块只回答「这份草稿要不要先别提交」，
 * 不提供「允许发布」之类的结论。
 */

export type SampleCategory = "positive" | "near_miss" | "out_of_scope";

export interface SampleInput {
  sample_id: string;
  category: SampleCategory;
  text: string;
}

/** 本地提示用的下限，与 domain/intent_sample.py 的 MIN_* 对齐。 */
export const LOCAL_MIN_POSITIVE = 3;
export const LOCAL_MIN_NEAR_MISS = 2;
/** 超范围例建议写，但服务端不设下限，本地也不把它算作缺口。 */
export const LOCAL_ADVISED_OUT_OF_SCOPE = 1;

export const SAMPLE_ID_MAX = 128;
export const SAMPLE_TEXT_MAX = 500;

export const SAMPLE_CATEGORIES: readonly SampleCategory[] = [
  "positive",
  "near_miss",
  "out_of_scope",
];

export const CATEGORY_LABEL: Record<SampleCategory, string> = {
  positive: "正例",
  near_miss: "近似误例",
  out_of_scope: "超范围例",
};

export interface SampleProblem {
  /** 与服务端 fields.loc 同形，例如 intent_samples.2.text，界面可标到具体输入框。 */
  loc: string;
  message: string;
}

export interface SampleCheck {
  ready: boolean;
  counts: Record<SampleCategory, number>;
  /** 「缺哪一类、缺几条」，一条一项。 */
  shortfalls: string[];
  problems: SampleProblem[];
  /** 不阻断、但值得提醒的事（例如建议补超范围例）。 */
  advisory: string[];
}

export interface SampleGateDecision {
  /** 直接绑定到「保存草稿」按钮的 disabled。 */
  disabled: boolean;
  className: "ok" | "warn";
  html: string;
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

/**
 * 校验一份草稿里的意图样例。
 *
 * 全空的占位行不报错也不计数：表单默认摆出 3 条正例 + 2 条近似误例的空行，
 * 此时应当只提示「还缺几条」，而不是同时报 5 个「不能为空」。
 */
export function checkSamples(rows: readonly SampleInput[]): SampleCheck {
  const counts: Record<SampleCategory, number> = {
    positive: 0,
    near_miss: 0,
    out_of_scope: 0,
  };
  const problems: SampleProblem[] = [];
  const seenIds = new Set<string>();
  const seenTexts = new Set<string>();

  rows.forEach((row, index) => {
    const id = row.sample_id.trim();
    const text = row.text.trim();
    const at = (field: string): string => `intent_samples.${index}.${field}`;
    const categoryOk = SAMPLE_CATEGORIES.includes(row.category);
    const before = problems.length;

    if (!categoryOk) {
      problems.push({ loc: at("category"), message: "类别必须是三类样例之一。" });
    }
    if (id === "" && text === "" && categoryOk) {
      return; // 尚未填写的占位行
    }
    if (id === "") {
      problems.push({ loc: at("sample_id"), message: "sample_id 不能为空。" });
    } else if (id.length > SAMPLE_ID_MAX) {
      problems.push({ loc: at("sample_id"), message: `sample_id 不能超过 ${SAMPLE_ID_MAX} 字。` });
    } else if (seenIds.has(id)) {
      problems.push({ loc: at("sample_id"), message: `sample_id「${id}」重复。` });
    } else {
      seenIds.add(id);
    }
    if (text === "") {
      problems.push({ loc: at("text"), message: "话术不能为空。" });
    } else if (text.length > SAMPLE_TEXT_MAX) {
      problems.push({ loc: at("text"), message: `话术不能超过 ${SAMPLE_TEXT_MAX} 字。` });
    } else if (seenTexts.has(text)) {
      problems.push({ loc: at("text"), message: "话术与前面某条完全相同，请换一种说法。" });
    } else {
      seenTexts.add(text);
    }

    // 只有形状合法、类别合法、且本行没有新增错误的样例才计入条数。
    // 一条填了一半的正例不该被算作「已有 1 条正例」，否则缺口会少报。
    if (categoryOk && problems.length === before) {
      counts[row.category] += 1;
    }
  });

  const shortfalls: string[] = [];
  if (counts.positive < LOCAL_MIN_POSITIVE) {
    shortfalls.push(
      `正例不足：${counts.positive}/${LOCAL_MIN_POSITIVE}，还缺 ` +
        `${LOCAL_MIN_POSITIVE - counts.positive} 条`,
    );
  }
  if (counts.near_miss < LOCAL_MIN_NEAR_MISS) {
    shortfalls.push(
      `近似误例不足：${counts.near_miss}/${LOCAL_MIN_NEAR_MISS}，还缺 ` +
        `${LOCAL_MIN_NEAR_MISS - counts.near_miss} 条`,
    );
  }

  const advisory: string[] = [];
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

/** 把预检结论翻译成界面状态：按钮禁用、状态条样式与逐条文案。 */
export function sampleGateDecision(check: SampleCheck): SampleGateDecision {
  const disabled = !check.ready;
  const title = check.ready
    ? `本地预检通过：正例 ${check.counts.positive} 条、近似误例 ` +
      `${check.counts.near_miss} 条、超范围例 ${check.counts.out_of_scope} 条。`
    : "本地预检未通过，「保存草稿」已禁用：";
  const items = [
    ...check.shortfalls.map((item) => `<li>缺：${escapeHtml(item)}</li>`),
    ...check.problems.map(
      (item) => `<li>${escapeHtml(item.loc)}：${escapeHtml(item.message)}</li>`,
    ),
    ...check.advisory.map((item) => `<li>${escapeHtml(item)}</li>`),
  ];
  const list = items.length > 0 ? `<ul>${items.join("")}</ul>` : "";
  const authority =
    '<div style="margin-top:6px">本地条数只是提示，最终以服务端发布门禁的 409 报告为准。</div>';
  return {
    disabled,
    className: check.ready ? "ok" : "warn",
    html: `<strong>${escapeHtml(title)}</strong>${list}${authority}`,
  };
}
