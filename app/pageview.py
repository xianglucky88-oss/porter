"""页面结构快照（PageView）。

定位决策只依赖 PageView 这份纯数据，不依赖具体驱动：
  * HttpDriver    —— 解析 HTTP GET 拿到的表单页 HTML；
  * BrowserDriver —— 解析 page.content() 拿到的当前 DOM HTML。

两条路径共用本模块同一个解析器，因此同一页面在两种驱动下产出
完全相同的 PageView，后续 resolver 的决策也就天然一致。

PageView 同时承担"页面版本特征"的职责：fingerprint 与 layout_kind
会写入执行证据，维护人员据此判断目标系统是否发生了改版。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

# 参与"label 与控件同容器"判定的块级标签。取最近的匹配祖先，
# 容器内多于一个控件时拒绝建立关联（防止写入相邻字段）。
_CONTAINER_TAGS = {"p", "div", "td", "th", "li", "tr", "fieldset"}

_CONTROL_TAGS = {"input", "textarea", "select"}

# 这些 input 类型不是业务字段（隐藏域、按钮），不进入字段列表。
_SKIP_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}


def _norm_text(text: str) -> str:
    """label / 按钮文本规范化：压缩空白，去掉必填星号与结尾冒号。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = t.rstrip("*").rstrip(":").rstrip("：").strip()
    return t


@dataclass
class Control:
    """一个可填写控件（input / textarea / select）。"""

    tag: str
    ctype: str  # input 的 type；textarea/select 取标签名
    cid: str | None
    name: str | None
    data_field: str | None
    container: str  # 最近块级容器描述，如 "div.form-row"；无则为 ""
    in_form: bool

    @property
    def signature(self) -> str:
        """用于指纹与证据的结构特征，不含任何业务取值。"""
        return _control_signature(self.tag, self.ctype, self.cid, self.name, self.data_field)

    def css(self) -> str:
        """为该控件生成确定性 CSS：优先 id，其次 name，再次 data-field。"""
        if self.cid:
            return f"#{self.cid}"
        if self.name:
            return f'{self.tag}[name="{self.name}"]'
        if self.data_field:
            return f'{self.tag}[data-field="{self.data_field}"]'
        # 三无控件无法稳定寻址；调用方应视为不可定位。
        return ""


@dataclass
class Label:
    text: str  # 规范化后的文本
    for_id: str | None
    wraps_control: bool  # label 是否直接包裹控件
    container: str
    # 被包裹控件的结构签名（Control.signature）；未包裹时为 None。
    # 用于把 label 精确关联到唯一控件，而不是靠容器位置猜测。
    wrapped_signature: str | None = None


@dataclass
class Button:
    tag: str  # button 或 input
    cid: str | None
    btype: str  # submit / button / ""
    text: str  # button 文本或 input[value]，规范化
    classes: str
    in_form: bool

    @property
    def signature(self) -> str:
        return f"{self.tag}(type={self.btype},id={self.cid or ''},text={self.text!r})"


@dataclass
class PageView:
    """一次页面快照。resolver 的唯一输入。"""

    url: str
    controls: list[Control] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    form_ids: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """页面版本特征指纹。结构不变则指纹不变；任何字段/控件/标签
        增删或属性变化都会改变指纹。只含结构信息，不含业务取值。"""
        sig = {
            "controls": sorted(c.signature for c in self.controls),
            "labels": sorted(lb.text for lb in self.labels),
            "buttons": sorted(b.signature for b in self.buttons),
            "forms": sorted(self.form_ids),
        }
        raw = json.dumps(sig, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def layout_kind(self) -> str:
        """对页面结构风格的粗分类，写入证据便于人工识别改版。"""
        if not self.controls:
            return "no-controls"
        if all(c.data_field for c in self.controls):
            return "data-field-layout"
        if any(lb.wraps_control for lb in self.labels):
            return "label-wrapped-layout"
        if all(c.cid for c in self.controls) and any(lb.for_id for lb in self.labels):
            return "id-label-layout"
        return "mixed-layout"

    def field_names(self) -> list[str]:
        """页面上可用于寻址的字段键集合（证据用）。"""
        keys = set()
        for c in self.controls:
            if c.data_field:
                keys.add(c.data_field)
            if c.name:
                keys.add(c.name)
            if c.cid:
                keys.add(c.cid)
        return sorted(keys)


# ---------------------------------------------------------------------------
# HTML 解析：构建轻量节点树后提取结构。
# ---------------------------------------------------------------------------


class _Node:
    __slots__ = ("attrs", "children", "parent", "seq", "tag", "texts")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None, seq: int) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.texts: list[str] = []
        self.parent = parent
        # 文档顺序号：让"同标签同 class 的不同容器"仍可区分，
        # 否则"同容器唯一控件"规则会把相邻段落的控件混为一谈。
        self.seq = seq

    def text(self) -> str:
        parts = list(self.texts)
        for ch in self.children:
            parts.append(ch.text())
        return " ".join(p for p in parts if p)

    def iter(self) -> Iterator[_Node]:
        yield self
        for ch in self.children:
            yield from ch.iter()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._seq = 0
        self.root = self._new_node("#root", {}, None)
        self.stack = [self.root]

    def _new_node(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> _Node:
        self._seq += 1
        return _Node(tag, attrs, parent, self._seq)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._new_node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        # void 元素没有结束标签，不入栈
        if tag not in ("input", "br", "hr", "img", "meta", "link"):
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._new_node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        # 容错：找到最近的匹配开标签再出栈
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            self.stack[-1].texts.append(data.strip())


def _control_signature(
    tag: str, ctype: str, cid: str | None, name: str | None, data_field: str | None
) -> str:
    return (
        f"{tag}(type={ctype},id={cid or ''},"
        f"name={name or ''},data-field={data_field or ''})"
    )


def _container_of(node: _Node) -> str:
    cur = node.parent
    while cur is not None:
        if cur.tag in _CONTAINER_TAGS:
            cls = (cur.attrs.get("class") or "").split()
            desc = f"{cur.tag}.{cls[0]}" if cls else cur.tag
            return f"{desc}@{cur.seq}"
        cur = cur.parent
    return ""


def _in_form(node: _Node) -> bool:
    cur = node.parent
    while cur is not None:
        if cur.tag == "form":
            return True
        cur = cur.parent
    return False


def parse_page(html_text: str, url: str = "") -> PageView:
    """把表单页 HTML 解析为 PageView。两种驱动共用此入口。"""
    builder = _TreeBuilder()
    builder.feed(html_text or "")
    root = builder.root

    page = PageView(url=url)

    for node in root.iter():
        if node.tag == "form":
            page.form_ids.append(node.attrs.get("id", ""))

    for node in root.iter():
        if node.tag not in _CONTROL_TAGS:
            continue
        ctype = node.attrs.get("type", "text") if node.tag == "input" else node.tag
        if node.tag == "input" and ctype.lower() in _SKIP_INPUT_TYPES:
            continue
        page.controls.append(
            Control(
                tag=node.tag,
                ctype=ctype,
                cid=node.attrs.get("id") or None,
                name=node.attrs.get("name") or None,
                data_field=node.attrs.get("data-field") or None,
                container=_container_of(node),
                in_form=_in_form(node),
            )
        )

    for node in root.iter():
        if node.tag != "label":
            continue
        wrapped_sig: str | None = None
        wraps = False
        for ch in node.children:
            if ch.tag in _CONTROL_TAGS:
                wraps = True
                wrapped_sig = _control_signature(
                    ch.tag,
                    ch.attrs.get("type", "text") if ch.tag == "input" else ch.tag,
                    ch.attrs.get("id") or None,
                    ch.attrs.get("name") or None,
                    ch.attrs.get("data-field") or None,
                )
                break
        page.labels.append(
            Label(
                text=_norm_text(node.text()),
                for_id=node.attrs.get("for") or None,
                wraps_control=wraps,
                container=_container_of(node),
                wrapped_signature=wrapped_sig,
            )
        )

    for node in root.iter():
        if node.tag == "button":
            page.buttons.append(
                Button(
                    tag="button",
                    cid=node.attrs.get("id") or None,
                    btype=(node.attrs.get("type") or "submit").lower(),
                    text=_norm_text(node.text()),
                    classes=node.attrs.get("class") or "",
                    in_form=_in_form(node),
                )
            )
        elif node.tag == "input" and (node.attrs.get("type") or "").lower() in ("submit", "button"):
            page.buttons.append(
                Button(
                    tag="input",
                    cid=node.attrs.get("id") or None,
                    btype=(node.attrs.get("type") or "").lower(),
                    text=_norm_text(node.attrs.get("value") or ""),
                    classes=node.attrs.get("class") or "",
                    in_form=_in_form(node),
                )
            )

    return page
