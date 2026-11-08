"""流程定义的意图样例：发布前的必备材料。

样例与流程版本存在一起（`FlowDefinition.intent_samples`），因为必须和"发布的是哪一版"
绑死。单独一张表会让"发布时校验的样例"与"运行时生效的版本"在两处漂移，而漂移的表现
恰好是最难查的那种：样例都通过了，线上群众说话却一直被要求澄清。

三类样例各自只断言一件事，都能被判定：

| 类别 | 断言 | 为什么只断言这一件 |
|---|---|---|
| `positive` | 必须被识别到本流程的**这一版** | 漏掉就是"群众说了话，系统说没听懂" |
| `near_miss` | 不得被识别到任何流程 | 话术很像但不是本事项；若真属于别的事项，就该写进那个事项 |
| `out_of_scope` | 不得被识别到任何流程 | 寒暄与无关请求不许被塞进任何执行参数 |

样例一律**合成或脱敏**，不得写入群众原话：它随版本入库，还会进评测报告与 CI 日志。
报告与错误信息只出现 `sample_id` 与类别，不出现原文。

条数下限取自骨架自检流程的基线：3 条正例覆盖三种常见说法，2 条近似误例足以逼出
"关键词太宽"这一类问题。这两个数字是**下限**而不是目标，真正的覆盖度由业务责任人补。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

# 发布所需的最少样例条数。与 `Settings.intent_eval_min_positive` 等默认值同源：
# 门禁与 CI 用同一套数字，否则"本地过了 CI 不过"只会被当成环境问题。
MIN_POSITIVE_SAMPLES = 3
MIN_NEAR_MISS_SAMPLES = 2


class IntentSampleCategory(StrEnum):
    POSITIVE = "positive"
    NEAR_MISS = "near_miss"
    OUT_OF_SCOPE = "out_of_scope"


CATEGORY_LABELS: dict[IntentSampleCategory, str] = {
    IntentSampleCategory.POSITIVE: "正例",
    IntentSampleCategory.NEAR_MISS: "近似误例",
    IntentSampleCategory.OUT_OF_SCOPE: "超范围例",
}


class IntentSample(BaseModel):
    """一条意图样例。是数据，不是代码：跟着流程版本一起存库。

    `sample_id` 只用于报告与排查（例如 `pos_selfcheck_a`），不必可读，但必须在同一
    `(flow_id, version)` 内唯一——报告里只出现它，不出现原文，出错时才能指得准。
    """

    sample_id: str = Field(min_length=1, max_length=128)
    category: IntentSampleCategory
    text: str = Field(min_length=1, max_length=500)


class SampleCoverage(BaseModel):
    """某一类样例的覆盖情况。`need` 来自门禁配置，`have` 来自版本内容。"""

    flow_id: str
    flow_version: str
    category: IntentSampleCategory
    have: int = Field(ge=0)
    need: int = Field(ge=0)

    @property
    def short(self) -> bool:
        return self.have < self.need

    @property
    def label(self) -> str:
        return CATEGORY_LABELS[self.category]

    def describe(self) -> str:
        return f"{self.label} {self.have}/{self.need}"


def count_by_category(samples: Iterable[IntentSample]) -> dict[IntentSampleCategory, int]:
    counts = dict.fromkeys(IntentSampleCategory, 0)
    for sample in samples:
        counts[sample.category] += 1
    return counts


def sample_coverage(
    samples: Sequence[IntentSample],
    *,
    flow_id: str,
    flow_version: str,
    min_positive: int = MIN_POSITIVE_SAMPLES,
    min_near_miss: int = MIN_NEAR_MISS_SAMPLES,
) -> tuple[SampleCoverage, ...]:
    """只对**有下限要求**的两类给出覆盖行。超范围例建议写，但不是发布前置条件。"""

    counts = count_by_category(samples)
    return (
        SampleCoverage(
            flow_id=flow_id,
            flow_version=flow_version,
            category=IntentSampleCategory.POSITIVE,
            have=counts[IntentSampleCategory.POSITIVE],
            need=max(0, min_positive),
        ),
        SampleCoverage(
            flow_id=flow_id,
            flow_version=flow_version,
            category=IntentSampleCategory.NEAR_MISS,
            have=counts[IntentSampleCategory.NEAR_MISS],
            need=max(0, min_near_miss),
        ),
    )
