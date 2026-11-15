"""字段定位解析器：多候选、确定性回退、歧义拒绝。

设计约束（来自业务要求）：
  * 页面会改版：字段标签、容器层级、可点击控件都可能变化，因此
    定位不能只依赖单一 CSS 选择器；
  * 错误的宽松匹配会把金额、申请人、供应商写进相邻字段，因此
    每一级候选都必须"唯一命中"才可采用；
  * 回退有确定优先级：id → name → data-field → label 关联；
  * 任何一级出现多个命中即为歧义：立即拒绝，绝不继续猜测，
    也绝不带着不确定的定位结果进入提交。

resolver 只消费 PageView（纯数据），与驱动无关——HTTP 驱动与
浏览器驱动面对同一页面会得到完全相同的定位决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pageview import Control, PageView

# 提交控件已知的稳定 id（历史版本沉淀）。按优先级排列。
SUBMIT_IDS = ["submit-btn", "submit", "primary-action", "do-submit"]

# 提交按钮可接受的可见文本（精确匹配，规范化后）。
SUBMIT_TEXTS = {"提交", "submit", "Submit", "确认提交"}

# 字段定位策略的确定优先级。顺序即回退顺序，不允许调整。
FIELD_STRATEGIES = ["id", "name", "data-field", "label"]


@dataclass
class Attempt:
    """一级候选策略的评估记录（证据）。"""

    strategy: str
    matches: list[str] = field(default_factory=list)  # 命中控件的结构特征，不含取值

    @property
    def count(self) -> int:
        return len(self.matches)


@dataclass
class Resolution:
    """一次定位的结果。ok=False 时 css 为 None，调用方必须失败退出。"""

    key: str
    ok: bool
    css: str | None = None
    strategy: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    failure: str | None = None  # "field_missing" | "ambiguous_match"
    detail: str | None = None

    def to_evidence(self) -> dict:
        return {
            "key": self.key,
            "ok": self.ok,
            "strategy": self.strategy,
            "css": self.css,
            "attempts": [
                {"strategy": a.strategy, "matches": a.count, "candidates": a.matches}
                for a in self.attempts
            ],
            "failure": self.failure,
            "detail": self.detail,
        }


def _css_for(control: Control, strategy: str, key: str) -> str:
    """为命中控件生成确定性 CSS：与命中策略同源。

    用哪一级属性命中，就用哪一级属性构造选择器——该属性已验证
    在页面上唯一，比组件库自动生成的 id 更稳定。
    """
    if strategy == "id" and control.cid:
        return f"#{control.cid}"
    if strategy == "name" and control.name:
        return f'{control.tag}[name="{control.name}"]'
    if strategy == "data-field" and control.data_field:
        return f'{control.tag}[data-field="{control.data_field}"]'
    # label 等间接策略：回退到控件自身最稳定的可寻址属性
    return control.css()


def _decide(
    key: str, attempts: list[Attempt], hit: Control | None, strategy: str | None
) -> Resolution:
    if hit is not None and strategy is not None:
        return Resolution(
            key=key, ok=True, css=_css_for(hit, strategy, key), strategy=strategy, attempts=attempts
        )
    return Resolution(
        key=key,
        ok=False,
        attempts=attempts,
        failure="field_missing",
        detail=f"no candidate matched field {key!r} after {len(attempts)} strategies",
    )


def _ambiguous(key: str, attempts: list[Attempt], strategy: str, matches: list[str]) -> Resolution:
    return Resolution(
        key=key,
        ok=False,
        attempts=attempts,
        failure="ambiguous_match",
        detail=(
            f"strategy {strategy!r} matched {len(matches)} controls for {key!r}; "
            "refusing to guess"
        ),
    )


def resolve_field(page: PageView, key: str, labels: list[str] | None = None) -> Resolution:
    """按确定优先级解析业务字段。

    labels：该字段已知的可见标签文本（精确匹配用），来自流程定义
    或字段注册表。label 策略只在前面三级全部零命中时启用。
    """
    attempts: list[Attempt] = []

    # 1) id 精确匹配
    hits = [c for c in page.controls if c.cid == key]
    attempts.append(Attempt("id", [c.signature for c in hits]))
    if len(hits) > 1:
        return _ambiguous(key, attempts, "id", attempts[-1].matches)
    if len(hits) == 1:
        return _decide(key, attempts, hits[0], "id")

    # 2) name 精确匹配
    hits = [c for c in page.controls if c.name == key]
    attempts.append(Attempt("name", [c.signature for c in hits]))
    if len(hits) > 1:
        return _ambiguous(key, attempts, "name", attempts[-1].matches)
    if len(hits) == 1:
        return _decide(key, attempts, hits[0], "name")

    # 3) data-field 精确匹配
    hits = [c for c in page.controls if c.data_field == key]
    attempts.append(Attempt("data-field", [c.signature for c in hits]))
    if len(hits) > 1:
        return _ambiguous(key, attempts, "data-field", attempts[-1].matches)
    if len(hits) == 1:
        return _decide(key, attempts, hits[0], "data-field")

    # 4) label 关联：label 文本精确等于已知标签，且能唯一关联到控件。
    #    关联只允许三种确定关系：for 属性 / 直接包裹 / 同容器唯一控件。
    #    不做"就近猜测"——这正是防止写入相邻字段的防线。
    hits = _resolve_by_label(page, labels or [])
    attempts.append(Attempt("label", [c.signature for c in hits]))
    if len(hits) > 1:
        return _ambiguous(key, attempts, "label", attempts[-1].matches)
    if len(hits) == 1:
        return _decide(key, attempts, hits[0], "label")

    return _decide(key, attempts, None, None)


def _resolve_by_label(page: PageView, expected: list[str]) -> list[Control]:
    """label 文本精确匹配（空白/星号/冒号规范化后）→ 控件。"""
    if not expected:
        return []
    wanted = {t for t in expected if t}
    out: list[Control] = []
    for lab in page.labels:
        if lab.text not in wanted:
            continue
        target: Control | None = None
        if lab.for_id:
            found = [c for c in page.controls if c.cid == lab.for_id]
            if len(found) == 1:
                target = found[0]
            elif len(found) > 1:
                # for 指向多个控件是页面缺陷，按歧义处理：全部计入命中
                out.extend(found)
                continue
        if target is None and lab.wrapped_signature:
            found = [c for c in page.controls if c.signature == lab.wrapped_signature]
            if len(found) == 1:
                target = found[0]
        if target is None and lab.container:
            # 同容器唯一控件才允许关联；容器内多控件时拒绝猜测
            siblings = [c for c in page.controls if c.container == lab.container]
            if len(siblings) == 1:
                target = siblings[0]
        if target is not None and target not in out:
            out.append(target)
    return out


def resolve_submit(page: PageView) -> Resolution:
    """解析提交控件。规则与字段一致：唯一命中才采用，歧义即拒绝。"""
    key = "submit"
    attempts: list[Attempt] = []

    # 1) 已知稳定 id（多个已知 id 同页时按 SUBMIT_IDS 顺序取优先级，
    #    但同一优先级下必须唯一）
    hits = [b for b in page.buttons if b.cid in SUBMIT_IDS]
    attempts.append(Attempt("known-id", [b.signature for b in hits]))
    if hits:
        best: list = []
        for sid in SUBMIT_IDS:
            best = [b for b in hits if b.cid == sid]
            if best:
                break
        if len(best) > 1:
            return _ambiguous(key, attempts, "known-id", [b.signature for b in best])
        return Resolution(
            key=key, ok=True, css=f"#{best[0].cid}", strategy="known-id", attempts=attempts
        )

    # 2) 表单内唯一 submit 类型控件
    hits = [b for b in page.buttons if b.btype == "submit" and b.in_form]
    attempts.append(Attempt("submit-type", [b.signature for b in hits]))
    if len(hits) > 1:
        return _ambiguous(key, attempts, "submit-type", attempts[-1].matches)
    if len(hits) == 1:
        btn = hits[0]
        css = f"#{btn.cid}" if btn.cid else f'{btn.tag}[type="submit"]'
        return Resolution(key=key, ok=True, css=css, strategy="submit-type", attempts=attempts)

    # 3) 可见文本精确匹配（唯一）
    hits = [b for b in page.buttons if b.text in SUBMIT_TEXTS]
    attempts.append(Attempt("text", [b.signature for b in hits]))
    if len(hits) > 1:
        return _ambiguous(key, attempts, "text", attempts[-1].matches)
    if len(hits) == 1:
        btn = hits[0]
        if btn.cid:
            css = f"#{btn.cid}"
        elif btn.tag == "input":
            css = f'input[type="{btn.btype}"]'
        else:
            css = "button"
        return Resolution(key=key, ok=True, css=css, strategy="text", attempts=attempts)

    return Resolution(
        key=key,
        ok=False,
        attempts=attempts,
        failure="field_missing",
        detail="no submit control matched after known-id/submit-type/text strategies",
    )
