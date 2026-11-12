"""选择器表。

历史包袱说明：这一版把选择器写成了模块级常量，全部是 CSS 选择器，
并且假定 mockapp 的 DOM 结构永远不变。mockapp 2.6 改过一次按钮 id
（`submit` → `submit-btn`），流程全线崩溃过一次，当时是手工热修。

之后的改进方案（未落地）是引入多候选定位与命中率统计，
"""

from __future__ import annotations

# 单候选，无备选。改版即崩。
SELECTORS: dict[str, str] = {
    "form": "form#entry-form",
    "source_key": "input#source_key",
    "submit": "button#submit-btn",
    "receipt": "p#receipt-id",
}

# 字段输入框是动态生成的，id 就是字段名。
FIELD_SELECTOR_TEMPLATE = "input#{name}"


def field_selector(name: str) -> str:
    return FIELD_SELECTOR_TEMPLATE.format(name=name)
