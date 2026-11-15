"""选择器表（兼容层）。

历史包袱说明：最早的版本把选择器写成模块级常量，全部是单候选 CSS，
并且假定 mockapp 的 DOM 结构永远不变。mockapp 2.6 改过一次按钮 id
（`submit` → `submit-btn`），流程全线崩溃过一次，当时是手工热修。

定位能力已迁移到 app/pageview.py（页面快照）与 app/resolver.py
（多候选 + 确定性回退 + 歧义拒绝）。本模块只保留旧调用方的兼容
入口，新代码不应再引用。
"""

from __future__ import annotations

import warnings

# 单候选，无备选。改版即崩。仅为兼容保留，不要在新流程中使用。
SELECTORS: dict[str, str] = {
    "form": "form#entry-form",
    "source_key": "input#source_key",
    "submit": "button#submit-btn",
    "receipt": "p#receipt-id",
}

# 字段输入框是动态生成的，id 就是字段名。
FIELD_SELECTOR_TEMPLATE = "input#{name}"


def field_selector(name: str) -> str:
    """旧版单候选选择器。新流程请使用 Step.field + resolver。"""
    warnings.warn(
        "field_selector() is deprecated; use Step.field with resolver instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return FIELD_SELECTOR_TEMPLATE.format(name=name)
