"""业务字段注册表：逻辑字段名 → 已知可见标签。

label 策略是定位回退链的最后一级，只在 id/name/data-field 全部
失效时启用。标签文本必须与页面 label 精确相等（规范化后），
因此这里维护的是"业务上被认可的字段叫法"，而不是模糊关键词。

流程 YAML 中的 fields: 映射可以覆盖/补充这里的默认值。
"""

from __future__ import annotations

# 默认值来自三个内部表单的业务约定。新增表单时在此登记。
DEFAULT_FIELD_LABELS: dict[str, list[str]] = {
    "source_key": ["业务单号"],
    "applicant": ["申请人"],
    "department": ["申请部门"],
    "supplier_code": ["供应商编码"],
    "amount_cents": ["金额（分）", "金额"],
    "budget_code": ["预算科目"],
    "expense_type": ["费用类型"],
    "invoice_no": ["发票号"],
    "remark": ["备注"],
    "company_name": ["企业名称"],
    "legal_person": ["法人"],
    "uscc": ["统一社会信用代码"],
    "bank_branch": ["开户网点"],
}


def labels_for(field: str, overrides: dict[str, list[str]] | None = None) -> list[str]:
    """字段的已知标签列表：流程覆盖优先，注册表默认兜底。"""
    if overrides and field in overrides:
        return list(overrides[field])
    return list(DEFAULT_FIELD_LABELS.get(field, []))
