/**
 * 群众侧状态投影：把服务端的 `TaskView` 变成一屏要显示什么。
 *
 * 这一层刻意做成**纯函数**，并且刻意不 import 任何东西：
 *
 * - 不读时钟、不设定时器、不看网络状态。所以"等动画放完就算办完了"这种写法
 *   在这里写不出来（`setTimeout` / `Date` 一个都没有）。完成与否只有一个来源：
 *   服务端这一次 `GET /api/tasks/{id}` 返回的 `ui_state`。
 * - 不改 `TaskView`，只做取舍与排序。派生规则全部可被 Node 直接回归
 *   （`tests/test_web_ui.py` 加载的就是编译产物 `dist/state_view.js`）。
 *
 * 文案也来自服务端（`ui_title` / `ui_hint`），页面不维护第二张"11 个状态怎么说"
 * 的表：多一份措辞就多一次"结果待确认被写成已办结"的机会。唯一的例外是
 * `user_action_required`（见下），服务端还没有对应的状态，页面只能用步骤作者
 * 写的原文顶上，且**不编造**去哪儿、多久过期。
 */

/** 服务端 `UiState` 的封闭集合（`domain/presentation.py`）。 */
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
] as const;

export type UiState = (typeof UI_STATES)[number];

/** 页面的一屏。「需要用户操作」不在服务端状态里，是按服务端事实派生出来的。 */
export type ScreenState = UiState | "user_action_required";

export type Connection = "live" | "reconnecting" | "offline";

export type ActionKind =
  | "start"
  | "answer"
  | "confirm"
  | "i_finished"
  | "cancel"
  | "restart"
  | "edit";

/** 服务端视图里页面用得到的部分。其余字段照样存在，只是这一层不需要。 */
export interface TaskView {
  task_id: string;
  conversation_id: string;
  flow_id: string;
  flow_version: string;
  /** 事项名（`FlowDefinition.title`）。版本缺失时为 `null`，页面不显示，也不猜。 */
  flow_title: string | null;
  status: string;
  current_step: string | null;
  current_step_title: string | null;
  current_step_prompt: string | null;
  confirm_message: string | null;
  completed_steps: string[];
  pending_fields: string[];
  field_labels: Record<string, string>;
  /**
   * 回显时要打码的字段名，**由流程作者声明**。页面不维护字段名黑名单：
   * 按名字猜"哪个像身份证"既会漏（真实业务里常叫 `contact`）又会错。
   * 没有声明就是明文——这个默认值由作者承担，不是页面替他判定。
   */
  sensitive_fields: string[];
  slots: Record<string, string>;
  result: Record<string, unknown> | null;
  failure_code: string | null;
  failure_message: string | null;
  ui_state: string;
  ui_title: string;
  ui_hint: string;
  updated_at: string;
}

export interface Field {
  name: string;
  label: string;
  value: string;
}

export interface SummaryRow {
  name: string;
  label: string;
  value: string;
}

/** 「需要用户操作」的三要素。`expires` 在没有服务端数据时只能说明"以页面为准"。 */
export interface Guide {
  what: string;
  where: string;
  expires: string;
}

export interface Action {
  kind: ActionKind;
  label: string;
  primary: boolean;
}

export interface Screen {
  state: ScreenState;
  title: string;
  hint: string;
  /** 澄清一次只问一个字段：这里最多一个，且顺序由服务端的 `pending_fields` 决定。 */
  field: Field | null;
  summary: SummaryRow[];
  /** 确认页要说明"影响"：取自流程作者的 `confirm_message`，不是页面拼的。 */
  impact: string | null;
  guide: Guide | null;
  actions: Action[];
  /** 按钮点了没用时，页面必须写明为什么，而不是留一个死按钮。 */
  blockedReason: string | null;
  /** 终态：不再有服务端写入动作。 */
  terminal: boolean;
}

export interface AnswerRequest {
  values: Record<string, string>;
}

export interface ConfirmRequest {
  step_id: string;
  accepted: true;
}

export interface ConnectionNotice {
  level: Connection;
  text: string;
}

const START: Action = { kind: "start", label: "开始办理", primary: true };
const SUBMIT: Action = { kind: "answer", label: "提交", primary: true };
const CONFIRM: Action = { kind: "confirm", label: "确认提交", primary: true };
const EDIT: Action = { kind: "edit", label: "返回修改", primary: false };
const CANCEL: Action = { kind: "cancel", label: "取消本次办理", primary: false };
const RESTART: Action = { kind: "restart", label: "重新发起", primary: true };
const I_FINISHED: Action = { kind: "i_finished", label: "我已完成", primary: true };

function emptyScreen(state: ScreenState, title: string, hint: string): Screen {
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

/**
 * 空闲屏。没有任何任务时才会出现，所以文案在这里（服务端此时没有任务可查，
 * 也没有 `TaskView` 可下发）。
 */
export function idleScreen(): Screen {
  return {
    ...emptyScreen("idle", "可以开始", "请描述您要办理的事情，说得口语一点也可以。"),
    actions: [START],
  };
}

function labelOf(view: TaskView, name: string): string {
  const label = view.field_labels[name];
  return label === undefined || label === "" ? name : label;
}

/** 已收集的字段，按服务端给的顺序。空值不列——那等于把"没填"当成"填了空"。 */
export function collected(view: TaskView): SummaryRow[] {
  const rows: SummaryRow[] = [];
  for (const [name, value] of Object.entries(view.slots)) {
    if (value === "") continue;
    rows.push({ name, label: labelOf(view, name), value });
  }
  return rows;
}

/**
 * 办结结果里能安全显示的部分：只取标量，跳过 `_` 开头的内部键。
 *
 * 不把整个 `result` 序列化进页面：里面可能有工具原始返回、内部单据标识。
 * 键名是否好懂由流程作者负责（P4 写试点流程时定稿）。
 */
export function resultRows(result: Record<string, unknown> | null): SummaryRow[] {
  if (result === null) return [];
  const rows: SummaryRow[] = [];
  for (const [name, value] of Object.entries(result)) {
    if (name.startsWith("_")) continue;
    const kind = typeof value;
    if (kind !== "string" && kind !== "number" && kind !== "boolean") continue;
    if (value === "") continue;
    rows.push({ name, label: name, value: String(value) });
  }
  return rows;
}

/** 澄清（含"返回修改"）一次只问一个字段，取服务端列出的第一个。 */
export function nextField(view: TaskView): Field | null {
  const name = view.pending_fields[0];
  if (name === undefined) return null;
  return { name, label: labelOf(view, name), value: view.slots[name] ?? "" };
}

export function fieldOf(view: TaskView, name: string): Field {
  return { name, label: labelOf(view, name), value: view.slots[name] ?? "" };
}

export function canSubmit(field: Field | null): boolean {
  return field !== null && field.value.trim().length > 0;
}

/**
 * 服务端 `ui_state` → 页面状态。**唯一的派生点。**
 *
 * 两处刻意的判断：
 * 1. `awaiting_input` 但服务端没列任何待填字段 = "在等用户，但不是等填表"，
 *    也就是 DESIGN §3.1 的「需要用户操作」。服务端还没有这个状态（P3 会在事件层
 *    补），页面按已有事实把它显示成"请到办理页面操作 + 我已完成"，而不是显示成
 *    一张空白表单。
 * 2. 认不出的 `ui_state`（服务端先上线了新状态）一律落到「状态待确认」：宁可
 *    少说，也不能落到「已完成」。
 */
export function screenStateOf(view: TaskView): ScreenState {
  const declared = UI_STATES.find((state) => state === view.ui_state);
  if (declared === undefined) return "outcome_unconfirmed";
  if (declared === "awaiting_input" && view.pending_fields.length === 0) {
    return "user_action_required";
  }
  return declared;
}

export function screenOf(view: TaskView | null): Screen {
  if (view === null) return idleScreen();
  const state = screenStateOf(view);
  const base = emptyScreen(state, view.ui_title, view.ui_hint);

  switch (state) {
    case "received":
    case "processing":
      // 执行中只给取消：重复提交在这里必须没有入口。
      return { ...base, actions: [CANCEL] };

    case "needs_clarification":
      // 服务端没能确定要办什么（未建任务）。页面回到输入框，不推进任何东西。
      return { ...base, actions: [{ kind: "start", label: "重新描述", primary: true }] };

    case "awaiting_input": {
      const field = nextField(view);
      const summary = collected(view);
      return {
        ...base,
        // 步骤作者写的「这一步要什么」比状态级的通用句具体得多：服务端给了就用它，
        // 群众看到的是「请补充需要办理的主题。」而不是「请补充提示的内容」。
        hint: view.current_step_prompt ?? base.hint,
        field,
        summary,
        actions: field === null ? [CANCEL] : [SUBMIT, CANCEL],
      };
    }

    case "awaiting_confirmation": {
      const summary = collected(view);
      const actions: Action[] = [CONFIRM];
      if (summary.length > 0) actions.push(EDIT);
      actions.push(CANCEL);
      return { ...base, summary, impact: view.confirm_message, actions };
    }

    case "user_action_required": {
      const field = nextField(view);
      return {
        ...base,
        // 服务端还没建模这个状态（P3 补事件）：标题用页面自己的说法，
        // 指引只重复流程作者写的步骤名与提示，不编造地址与倒计时。
        title: "需要您本人操作",
        hint: "请在办理页面完成操作，完成后回到这里继续。",
        field,
        guide: {
          what: view.current_step_title ?? "按页面提示完成操作",
          where: view.current_step_prompt ?? "请按办理页面上的提示操作",
          expires: "有效期以办理页面上的提示为准",
        },
        // 「我已完成」只走 `/answers`，永不签发确认凭证。今天这一屏的 field 必然
        // 为空（派生条件本身就是"没有待填字段"），所以按钮不启用并写明原因：
        // 点下去必然 422，那比"暂时不能点"更让人困惑。等 P3 的事件带上要提交的
        // 字段，同一条通道就是一次普通回答。
        actions: [I_FINISHED, CANCEL],
        blockedReason:
          field === null
            ? "当前没有需要提交的内容；在办理页面完成后，本页会自动更新。"
            : null,
      };
    }

    case "completed":
      return { ...base, summary: resultRows(view.result), actions: [RESTART], terminal: true };

    case "failed":
      // 失败原因不直接印 `failure_message`：那是工具/模型侧的原话。可确认的事实
      // 由服务端文案给，逐步骤的失败码留给 P3/P4 做群众口径映射。
      return { ...base, actions: [RESTART], terminal: true };

    case "outcome_unconfirmed":
      // 外部结果未知：既不是成功也不是失败，也**不给**"重新发起"——那等于
      // 引导群众重复提交一笔可能已经受理的申请。
      return { ...base, terminal: true };

    case "handoff":
      return { ...base, terminal: true };

    case "cancelled":
      return { ...base, actions: [RESTART], terminal: true };

    case "idle":
      return { ...base, actions: [START] };
  }
}

/**
 * 请求体构造。刻意只导出这两个：群众侧能写服务端的动作只有"回答"和"确认"，
 * 取消单独一条，其余是只读刷新。
 *
 * 「我已完成」走 `answerRequest`——它绝不构造 `step_id`/`accepted`，
 * 也就不可能被当成确认凭证（D3 红线）。
 */
export function answerRequest(field: Field): AnswerRequest {
  return { values: { [field.name]: field.value } };
}

export function confirmRequest(view: TaskView): ConfirmRequest {
  if (view.current_step === null) {
    throw new Error("当前没有可确认的步骤");
  }
  return { step_id: view.current_step, accepted: true };
}

/** 断线只影响这条提示，**不触碰任务状态**。 */
export function connectionNotice(connection: Connection): ConnectionNotice | null {
  if (connection === "live") return null;
  if (connection === "reconnecting") {
    return { level: "reconnecting", text: "连接中断，正在重新连接；任务可能仍在继续。" };
  }
  return { level: "offline", text: "无法连接服务端，请检查网络后重试。" };
}

// ---------- 对话记录 ----------

/**
 * 一条对话消息。`id` 由 `(状态, 步骤, 字段)` 决定，**跨次渲染稳定**：它是 DOM 复用的
 * 依据，也是"重渲染不重播"的依据（见 `main.ts` 的 `streamEl`）。
 */
export interface Message {
  id: string;
  kind: MessageKind;
  /** 助手侧的人设：开场、追问、结论。用户侧只有 `user_answer`。 */
  role: "assistant" | "user";
  /** 原样展示的正文。敏感字段的值在这里**已经是打码后的**。 */
  text: string;
}

export type MessageKind =
  /** 开场：您要办的是哪件事。 */
  | "opening"
  /** 服务端状态文案（`ui_title` / `ui_hint`），以及步骤作者的 `prompt`。 */
  | "assistant"
  /** 追问某个字段。`text` 是字段标签，`pending` 说明还没填。 */
  | "field_question"
  /** 群众已经答过的字段。`text` 永远是打码后的值。 */
  | "user_answer"
  /** 确认页：将要执行什么、影响是什么。 */
  | "confirmation"
  /** 办结结果的行。 */
  | "result"
  /** 终态的非成功措辞（失败 / 待确认 / 转人工 / 取消）。 */
  | "status"
  /** 「需要用户操作」的指引。 */
  | "guide";

/**
 * 打码。长度 ≥ 12 保留前 6 后 4，长度 6–11 保留后 4，`<= 4` 全掩码；中间一律掩掉。
 *
 * 阈值来自原型评审时那一屏：身份证 18 位要保住前 6 位（行政区划）与后 4 位，
 * 便于本人核对；手机号 11 位保住后 4 位——把它整串掩掉虽然"更安全"，但群众
 * 就无从确认自己填的是不是自己那一部。
 *
 * 刻意不是"每个字符都按比例掩"：条纹越多越像真值，越容易被截图后拼回去。
 */
export function maskValue(value: string): string {
  const chars = [...value];
  if (chars.length <= 4) return "*".repeat(chars.length);
  const tail = 4;
  const head = chars.length >= 12 ? 6 : 0;
  return (
    chars.slice(0, head).join("") +
    "*".repeat(chars.length - head - tail) +
    chars.slice(-tail).join("")
  );
}

function isSensitive(view: TaskView, name: string): boolean {
  return view.sensitive_fields.indexOf(name) >= 0;
}

/**
 * 打码后的值。**这是页面唯一一处把字段值转成可显示文本的地方**：
 * 只要所有回显都从这里过，就不可能出现"某条消息忘了打码"。
 */
export function displayValue(view: TaskView, name: string): string {
  const value = view.slots[name] ?? "";
  if (value === "") return "";
  return isSensitive(view, name) ? maskValue(value) : value;
}

function stepOf(view: TaskView): string {
  return view.current_step ?? "-";
}

/**
 * 由服务端视图派生整条对话记录。**不累积本地数组。**
 *
 * 为什么必须是纯派生：SSE 提醒、断线重连、刷新都会让派生跑很多次。页面自己记着
 * "我说过什么"，就会在重连后重复一遍、在刷新后全丢——而"页面自己记着进度"正是
 * 这套架构一直在防的东西。所以两次调用同一视图，输出必须深度相等。
 *
 * 刻意没有的东西：
 * - 没有时间戳（页面无时针，见 `test_projection_layer_cannot_see_time`）；
 * - 没有"正在输入"（那是按等待推断状态）；
 * - 没有原始自然语言（`TaskView` 里根本没有 `input_text`，也拿不到）。
 */
export function transcriptOf(view: TaskView | null): Message[] {
  const messages: Message[] = [];
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

  // 开场：事项名来自流程作者。缺失时不说"您要办的是：undefined"，只给状态文案。
  if (view.flow_title !== null && view.flow_title !== "") {
    messages.push({
      id: "opening:flow",
      kind: "opening",
      role: "assistant",
      text: `您要办的是：${view.flow_title}。`,
    });
  }

  // 澄清阶段没有任务，服务端连字段都没列：只出一条"没听清"，不发任何追问。
  if (state === "needs_clarification") {
    messages.push({
      id: "clarify:ask",
      kind: "assistant",
      role: "assistant",
      text: view.ui_hint,
    });
    return messages;
  }

  // 已答字段的问答对，顺序听服务端的 `slots`。已完成走到确认页也不丢——
  // 群众要能回头看自己前面答了什么。
  for (const name of Object.keys(view.slots)) {
    const shown = displayValue(view, name);
    if (shown === "") continue;
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

  if (
    state === "awaiting_input" ||
    state === "user_action_required" ||
    state === "received" ||
    state === "processing"
  ) {
    // 当前要填的字段：`pending_fields` 的第一个，与 `screenOf` 同源。
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
    } else if (state === "user_action_required") {
      messages.push({
        id: `guide:${stepOf(view)}:what`,
        kind: "guide",
        role: "assistant",
        text: `这一步需要您本人操作：${view.current_step_title ?? "请按页面提示完成操作"}。`,
      });
      messages.push({
        id: `guide:${stepOf(view)}:where`,
        kind: "guide",
        role: "assistant",
        text: view.current_step_prompt ?? "请按办理页面上的提示操作。",
      });
      messages.push({
        id: `guide:${stepOf(view)}:expires`,
        kind: "guide",
        role: "assistant",
        text: "有效期以办理页面上的提示为准。",
      });
    } else if (state === "received" || state === "processing") {
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
      text:
        rows.length === 0
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
    // 结果行之后**不再追加**任何"进行中"性质的尾巴：办完了就是办完了。
    messages.push({
      id: "status:completed",
      kind: "status",
      role: "assistant",
      text: view.ui_title,
    });
    return messages;
  }

  if (
    state === "failed" ||
    state === "outcome_unconfirmed" ||
    state === "handoff" ||
    state === "cancelled"
  ) {
    // 服务端文案本身就是非成功措辞（`domain/presentation.py` 定的），页面不改写、
    // 也不补一句"请重新提交"——"结果待确认"时那等于引导重复提交。
    messages.push({
      id: `status:${state}`,
      kind: "status",
      role: "assistant",
      text: view.ui_title,
    });
  }

  return messages;
}
