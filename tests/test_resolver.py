"""resolver 与 pageview 的纯单元测试。

不依赖运行中的服务：直接用 mockapp 的布局渲染器产出各版本 HTML，
验证同一业务流程在不同页面结构下的定位决策一致、确定、可解释。

覆盖的核心命题：
  * 三种布局（v1/v2/v3）下所有业务字段都能唯一定位；
  * 回退优先级确定：id → name → data-field → label；
  * 候选歧义时拒绝（绝不猜测），且证据里能看到冲突候选；
  * 相似标签不会串字段（"金额"不会写到"金额（分）"的邻居）；
  * 提交按钮在各布局下都能唯一定位。
"""

from __future__ import annotations

import pytest

from app.pageview import parse_page
from app.resolver import FIELD_STRATEGIES, resolve_field, resolve_submit
from mockapp.main import FORMS, LAYOUTS

SPEC = FORMS["purchase_requisition"]
FIELDS = [f["name"] for f in SPEC["fields"]] + ["source_key"]
LABELS = {f["name"]: [f["label"]] for f in SPEC["fields"]}
LABELS["source_key"] = ["业务单号"]


def _page(layout: str, form_type: str = "purchase_requisition"):
    html = LAYOUTS[layout](FORMS[form_type], form_type)
    return parse_page(html, url=f"/form/{form_type}")


@pytest.mark.parametrize("layout", ["v1", "v2", "v3"])
def test_all_fields_resolve_in_every_layout(layout: str):
    page = _page(layout)
    for name in FIELDS:
        res = resolve_field(page, name, LABELS[name])
        assert res.ok, f"{layout}/{name}: {res.detail}"
        assert res.css, f"{layout}/{name}: empty css"


def test_v1_prefers_id_strategy():
    page = _page("v1")
    res = resolve_field(page, "applicant", LABELS["applicant"])
    assert res.ok and res.strategy == "id"
    assert res.css == "#applicant"


def test_v2_falls_back_to_name():
    page = _page("v2")
    res = resolve_field(page, "applicant", LABELS["applicant"])
    assert res.ok and res.strategy == "name"
    assert res.css == 'input[name="applicant"]'


def test_v3_falls_back_to_data_field():
    page = _page("v3")
    res = resolve_field(page, "applicant", LABELS["applicant"])
    assert res.ok and res.strategy == "data-field"
    assert res.css == 'input[data-field="applicant"]'


def test_label_strategy_used_when_no_structural_hook():
    """id/name/data-field 全部失效时，label 精确匹配兜底。"""
    html = """<form id="entry-form">
      <p><label>申请人</label><input class="txt"></p>
      <p><label>金额（分）</label><input class="num"></p>
    </form>"""
    page = parse_page(html)
    res = resolve_field(page, "applicant", ["申请人"])
    assert res.ok and res.strategy == "label"


def test_label_match_is_exact_not_substring():
    """"金额"不能命中"金额（分）"——防止写入相邻字段的关键防线。"""
    html = """<form id="entry-form">
      <p><label>金额（分）</label><input class="a"></p>
      <p><label>金额大写</label><input class="b"></p>
    </form>"""
    page = parse_page(html)
    res = resolve_field(page, "amount_cents", ["金额"])
    assert not res.ok
    assert res.failure == "field_missing"


def test_ambiguous_duplicate_id_is_rejected():
    """主表单与批量填充区块出现重复 id+name：拒绝猜测。"""
    page = _page("ambiguous")
    res = resolve_field(page, "amount_cents", LABELS["amount_cents"])
    assert not res.ok
    assert res.failure == "ambiguous_match"
    # 证据里能看到冲突发生在哪一级、命中了几个候选
    id_attempt = next(a for a in res.attempts if a.strategy == "id")
    assert id_attempt.count == 2
    # 歧义时不得给出任何 css
    assert res.css is None


def test_ambiguity_does_not_fall_through_to_next_strategy():
    """某一级歧义即整体拒绝，不继续用更低优先级策略碰运气。"""
    html = """<form id="entry-form">
      <input id="x1" name="applicant">
      <input id="x2" name="applicant" data-field="applicant">
    </form>"""
    page = parse_page(html)
    res = resolve_field(page, "applicant", ["申请人"])
    assert not res.ok and res.failure == "ambiguous_match"
    # 只评估到 name 一级就拒绝，data-field 一级不应出现
    assert [a.strategy for a in res.attempts] == ["id", "name"]


def test_missing_field_reports_all_attempts():
    page = _page("v1")
    res = resolve_field(page, "nonexistent", ["不存在的标签"])
    assert not res.ok and res.failure == "field_missing"
    assert [a.strategy for a in res.attempts] == FIELD_STRATEGIES
    assert all(a.count == 0 for a in res.attempts)


def test_label_with_multiple_controls_in_container_is_not_guessed():
    """label 与多个控件同容器时，不允许"就近"关联。"""
    html = """<form id="entry-form">
      <div class="row"><label>申请人</label><input class="a"><input class="b"></div>
    </form>"""
    page = parse_page(html)
    res = resolve_field(page, "applicant", ["申请人"])
    assert not res.ok and res.failure == "field_missing"


def test_duplicate_exact_labels_are_ambiguous():
    html = """<form id="entry-form">
      <p><label>备注</label><input class="a"></p>
      <p><label>备注</label><input class="b"></p>
    </form>"""
    page = parse_page(html)
    res = resolve_field(page, "remark", ["备注"])
    assert not res.ok and res.failure == "ambiguous_match"


@pytest.mark.parametrize("layout", ["v1", "v2", "v3"])
def test_submit_control_resolves_in_every_layout(layout: str):
    page = _page(layout)
    res = resolve_submit(page)
    assert res.ok, f"{layout}: {res.detail}"
    assert res.css


def test_submit_ambiguity_rejected():
    html = """<form id="entry-form">
      <button type="submit">提交</button>
      <button type="submit">提交</button>
    </form>"""
    page = parse_page(html)
    res = resolve_submit(page)
    assert not res.ok and res.failure == "ambiguous_match"


def test_fingerprint_distinguishes_layouts_and_is_stable():
    fp = {layout: _page(layout).fingerprint for layout in ("v1", "v2", "v3")}
    assert len(set(fp.values())) == 3, "不同布局应有不同指纹"
    assert _page("v1").fingerprint == fp["v1"], "同一布局指纹必须稳定"


def test_fingerprint_changes_with_field_set():
    page_a = _page("v1", "purchase_requisition")
    page_b = _page("v1", "reimbursement")
    assert page_a.fingerprint != page_b.fingerprint


def test_resolution_is_deterministic():
    """同一页面上重复解析，决策必须完全一致（驱动一致性的基础）。"""
    page = _page("v2")
    first = resolve_field(page, "amount_cents", LABELS["amount_cents"])
    for _ in range(5):
        again = resolve_field(page, "amount_cents", LABELS["amount_cents"])
        assert (again.ok, again.css, again.strategy) == (first.ok, first.css, first.strategy)


def test_page_view_contains_no_field_values():
    """PageView 只含结构信息：指纹与特征中不应出现业务取值。"""
    html = """<form id="entry-form">
      <p><label for="amount_cents">金额（分）</label>
      <input id="amount_cents" name="amount_cents" value="1289000"></p>
    </form>"""
    page = parse_page(html)
    assert "1289000" not in page.fingerprint
    for c in page.controls:
        assert "1289000" not in c.signature
