"""意图识别与槽位抽取：服务端确定性契约。

这一层的失败模式与模型输出解析同源：**把"没听懂"读成"听懂了"**。误路由的代价
不是多问一句，而是群众的话被塞进另一个事项的执行参数里。因此本模块只允许做
三件事，且每一件都写在返回值里、可被测试固定：

1. 判断这句话最可能属于哪个**已注册**流程（未注册流程一律不返回）；
2. 抽出群众**已经说出口**的字段值（抽不到就不填，绝不按默认值补齐）；
3. 打平时判歧义（不同流程分值相同），交回上层澄清，而不是挑一个"最像的"。

刻意不做的事：不判断能不能办（那是流程定义与服务端校验的职责）、不编造政策、
不扩充未声明的字段、不因为话术礼貌就提高置信度。

匹配用归一化文本（NFKC + 空白压缩 + 小写），回写用归一化后的值：全角数字在
参数里应当是和半角一致的同一个值，否则同一份材料会在两处对不上。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from workpanda.application.ports import IntentMatch

# 单个槽位值的长度上限。超过上限一律当作"没抽到"，而不是截断：
# 被截断的值会被当作群众原话写进执行参数，比重问一次危险得多。
MAX_SLOT_VALUE_LENGTH = 200
# 打分并列的容差。浮点比较不能用 ==，否则"几乎一样"会被当成"明显更优"。
TIE_EPSILON = 1e-9
# 识别结果里最多保留几个候选意图用于排查（对外只给 intent_id，不给流程细节）。
MAX_CANDIDATES = 3

_ONLY_PUNCTUATION = re.compile(r"^[\W_]+$", re.UNICODE)


def normalize_text(text: str) -> str:
    """统一全角/半角与空白与大小写。只用于匹配与取值，不改动原文语义。"""

    folded = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", folded).strip().lower()


@dataclass(frozen=True)
class IntentRule:
    """一条意图规则：关键词打分 + 字段正则抽取。

    `slot_patterns` 的每个模式只在**归一化文本**上匹配；命中后取第 1 个捕获组
    （没有捕获组时取整个匹配）。一条规则只认它自己声明的字段，因此路由器永远
    不可能凭一句话产出流程没声明过的参数。

    写模式时**不要用定长上限**（如 `{1,60}`）：超长输入会被静默截断成"看起来
    合法"的值。应当一直取到分隔符/空白，再由 `MAX_SLOT_VALUE_LENGTH` 判超长，
    这样超长值是"没抽到"而不是"被截短后写进执行参数"。
    """

    intent_id: str
    flow_id: str
    flow_version: str
    keywords: tuple[str, ...]
    slot_patterns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # 命中一个关键词的基准分；每多命中一个加 `per_extra_hit`，封顶 `max_confidence`。
    base_confidence: float = 0.6
    per_extra_hit: float = 0.15
    max_confidence: float = 0.95

    def score(self, normalized_text: str) -> tuple[float, int]:
        """返回 (置信度, 命中关键词数)。命中 0 个即该规则不参与竞争。"""

        hits = sum(1 for keyword in self.keywords if keyword and keyword in normalized_text)
        if hits == 0:
            return 0.0, 0
        raw = self.base_confidence + self.per_extra_hit * (hits - 1)
        return min(self.max_confidence, raw), hits

    def extract(self, normalized_text: str) -> dict[str, str]:
        """按声明顺序抽字段。抽不到、值过长、值只有标点一律留空。"""

        slots: dict[str, str] = {}
        for name, patterns in self.slot_patterns.items():
            for pattern in patterns:
                match = re.search(pattern, normalized_text)
                if match is None:
                    continue
                value = match.group(1) if match.groups() else match.group(0)
                cleaned = _clean_value(value)
                if cleaned is not None:
                    slots[name] = cleaned
                    break
        return slots


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("：:，,。.;；、 ")
    if not cleaned or len(cleaned) > MAX_SLOT_VALUE_LENGTH:
        return None
    if _ONLY_PUNCTUATION.match(cleaned):
        return None
    return cleaned


class RouteOutcome(StrEnum):
    MATCHED = "matched"
    NO_MATCH = "no_match"
    # 两个及以上**不同流程**打平。挑一个等于替群众做决定，必须交回澄清。
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RouteDecision:
    """一次识别决策的完整记录，供上层与评测集使用。"""

    outcome: RouteOutcome
    match: IntentMatch | None = None
    candidates: tuple[str, ...] = ()
    score: float = 0.0

    def __post_init__(self) -> None:
        if self.outcome is RouteOutcome.MATCHED and self.match is None:
            raise ValueError("matched decision must carry a match")
        if self.outcome is not RouteOutcome.MATCHED and self.match is not None:
            raise ValueError("unmatched decision must not carry a match")


@dataclass
class RuleIntentRouter:
    """确定性规则路由器。实现 `IntentRouter` 端口，可直接替换占位实现。

    排序与并列都必须是确定的：同分时先按 `intent_id` 排，保证同一句话在两次
    运行里给出同一个结果——否则评测集本身就不可复现。
    """

    rules: Sequence[IntentRule] = ()
    tie_epsilon: float = TIE_EPSILON

    def __post_init__(self) -> None:
        self.rules = tuple(self.rules)

    @property
    def has_rules(self) -> bool:
        return bool(self.rules)

    def decide(self, text: str) -> RouteDecision:
        normalized = normalize_text(text)
        if not normalized:
            return RouteDecision(outcome=RouteOutcome.NO_MATCH)

        scored: list[tuple[float, int, IntentRule]] = []
        for rule in self.rules:
            score, hits = rule.score(normalized)
            if hits:
                scored.append((score, hits, rule))

        if not scored:
            return RouteDecision(outcome=RouteOutcome.NO_MATCH)

        best_score = max(item[0] for item in scored)
        tied = [item for item in scored if abs(item[0] - best_score) <= self.tie_epsilon]
        flows = {item[2].flow_id for item in tied}

        if len(flows) > 1:
            # 同一流程的不同规则打平不算歧义（业务事项是同一个）。
            candidates = tuple(
                sorted(
                    {item[2].intent_id for item in tied},
                )
            )[:MAX_CANDIDATES]
            return RouteDecision(
                outcome=RouteOutcome.AMBIGUOUS,
                candidates=candidates,
                score=best_score,
            )

        # 同一流程内打平：命中关键词多的优先，再按 intent_id 定序。
        chosen = sorted(tied, key=lambda item: (-item[1], item[2].intent_id))[0]
        rule = chosen[2]
        match = IntentMatch(
            intent_id=rule.intent_id,
            flow_id=rule.flow_id,
            flow_version=rule.flow_version,
            confidence=round(best_score, 4),
            slots=rule.extract(normalized),
        )
        return RouteDecision(outcome=RouteOutcome.MATCHED, match=match, score=best_score)

    def route(self, text: str) -> IntentMatch | None:
        """`IntentRouter` 端口实现。歧义与无命中都返回 `None`：

        两者对客户端是同一个动作——先澄清，问清是哪件事，再谈办理。
        需要区分原因时用 `decide()`，它不会把差异丢掉。
        """

        return self.decide(text).match
