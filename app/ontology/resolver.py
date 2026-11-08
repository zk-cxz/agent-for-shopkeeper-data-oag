"""确定性术语解析器 —— OAG 与 RAG 的核心分界点

RAG 的做法：把问题切词后丢给向量库取 Top-K，永远返回"最像的"，不会说"没有"。
本模块的做法：
  1) 全局最长匹配，命中即锚定到规范对象（指标 / 列 / 取值 / 实体）；
  2) 匹配后把已命中的字面"挖掉"，剩下的是**未登记词**；
  3) 未登记词非空 => 直接判定超纲，拒绝作答，而不是把最像的字段凑上去。

这一步全程不调大模型、不做相似度猜测，因此可复现、可审计。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import jieba
import jieba.posseg as pseg

from app.ontology.model import Ontology

# 匹配优先级：同长度时，指标 > 取值 > 列 > 实体 > 时间
_KIND_PRIORITY = {"metric": 0, "value": 1, "column": 2, "entity": 3, "time": 4, "relative": 5}
_MASK = "\u0001"

_COUNT_PATTERN = re.compile(r"多少|几个|几笔|几条|数量|单数|个数|笔数|条数|共有|总计|总共|一共")
# 分组意图：只有出现这些词，时间列才需要出现在 SELECT/GROUP BY 里
_GROUP_INTENT = re.compile(r"分别|各|每个|每|按|逐|趋势|分布")


@dataclass
class Resolution:
    question: str
    metrics: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    values: list[tuple[str, str]] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    time_label: str | None = None
    time_grain: str | None = None
    relative_time: str | None = None
    unresolved: list[str] = field(default_factory=list)
    matched_by: dict[str, str] = field(default_factory=dict)
    refused: bool = False
    refuse_reason: str | None = None

    @property
    def count_intent(self) -> bool:
        return bool(_COUNT_PATTERN.search(self.question))

    def summary(self) -> str:
        return (
            f"指标={self.metrics} 列={self.columns} 取值={[v for v, _ in self.values]} "
            f"实体={self.tables} 时间={self.time_label or self.relative_time or '-'} "
            f"未登记={self.unresolved}"
        )


def _normalize(text: str) -> str:
    return (text or "").replace("\u3000", " ").strip()


def _build_patterns(onto: Ontology) -> list[tuple[str, str, object]]:
    """把所有可匹配字面展开成 (surface, kind, payload)，供最长匹配使用"""
    pats: list[tuple[str, str, object]] = []

    for surface, name in onto.metric_index.items():
        if len(surface) >= 2:
            pats.append((surface, "metric", name))
    for surface, payload in onto.value_index.items():
        if len(surface) >= 2:
            pats.append((surface, "value", payload[0]))
    for surface, col_id in onto.column_index.items():
        if len(surface) >= 2 and "." not in surface:
            pats.append((surface, "column", col_id))
    for surface, table in onto.entity_aliases.items():
        if len(surface) >= 2:
            pats.append((surface, "entity", table))
    for surface, label in onto.time_index.items():
        pats.append((surface, "time", label))
    for surface in onto.lexicon.get("relative_times") or []:
        pats.append((surface, "relative", surface))

    # 最长优先；同长度按优先级
    pats.sort(key=lambda x: (-len(x[0]), _KIND_PRIORITY[x[1]]))
    return pats


def _already_masked(mask: list[bool], start: int, end: int) -> bool:
    return any(mask[start:end])


def _apply(res: Resolution, kind: str, payload: object, surface: str) -> None:
    if kind == "metric":
        if payload not in res.metrics:
            res.metrics.append(str(payload))
    elif kind == "value":
        value, col_id = payload  # type: ignore[misc]
        pair = (value, col_id)
        if pair not in res.values:
            res.values.append(pair)
        if col_id not in res.columns:
            res.columns.append(col_id)
    elif kind == "column":
        if payload not in res.columns:
            res.columns.append(str(payload))
    elif kind == "entity":
        if payload not in res.tables:
            res.tables.append(str(payload))
    elif kind == "time":
        res.time_label = str(payload)
    elif kind == "relative":
        res.relative_time = str(payload)
    res.matched_by[surface] = kind


def resolve(question: str, onto: Ontology, patterns: list | None = None) -> Resolution:
    """把自然语言问题解析成本体对象集合；无法锚定的部分判为超纲"""
    text = _normalize(question)
    res = Resolution(question=text)
    mask = [False] * len(text)
    pats = patterns if patterns is not None else _build_patterns(onto)

    for surface, kind, payload in pats:
        start = 0
        while True:
            idx = text.find(surface, start)
            if idx < 0:
                break
            end = idx + len(surface)
            if not _already_masked(mask, idx, end):
                for i in range(idx, end):
                    mask[i] = True
                _apply(res, kind, payload, surface)
            start = idx + 1

    # ---- 未命中的残片：分词后保留有实义的名词/动词，即为"未登记词" ----
    generic = set(onto.lexicon.get("generic_tokens") or [])
    drop_pos = set(onto.lexicon.get("drop_pos") or [])
    fragments: list[str] = []
    buf: list[str] = []
    for ch, hit in zip(text, mask):
        if hit:
            if buf:
                fragments.append("".join(buf))
                buf = []
            fragments.append(_MASK)
        else:
            buf.append(ch)
    if buf:
        fragments.append("".join(buf))

    unresolved: list[str] = []
    for frag in fragments:
        if frag == _MASK or not frag.strip():
            continue
        for word, flag in pseg.cut(frag):
            w = word.strip()
            if not w or flag in drop_pos:
                continue
            if w in generic or w in onto.metric_index or w in onto.column_index:
                continue
            if w in onto.value_index or w in onto.entity_aliases:
                continue
            if len(w) < 2:
                continue
            if w not in unresolved:
                unresolved.append(w)
    res.unresolved = unresolved

    # ---- 时间粒度：只有明确要求"分别/各/按…"分组时才设定 ----
    # 否则"2025年1月份的销售额"会被误当成"按月分组"，多输出一个月份列
    if _GROUP_INTENT.search(text):
        for grain_word in (onto.temporal.get("grain_columns") or {}):
            if grain_word in text:
                res.time_grain = grain_word
                break

    # ---- 判定：规则优先于模型 ----
    rules = onto.rules or {}
    if res.relative_time and rules.get("refuse_relative_time"):
        res.refused = True
        dr = onto.data_range or {}
        res.refuse_reason = (
            f"问题涉及相对时间「{res.relative_time}」，但数据只覆盖 "
            f"{dr.get('min_date')} ~ {dr.get('max_date')}，无法作答。"
        )
    elif unresolved:
        res.refused = True
        res.refuse_reason = (
            f"以下概念在数据字典中没有登记，无法映射到任何表或字段：{'、'.join(unresolved)}。"
            "请确认口径，或先在本体里登记该概念。"
        )
    elif not (res.metrics or res.columns or res.values or res.tables or res.time_label):
        res.refused = True
        res.refuse_reason = "未能从问题中识别出任何已登记的业务对象（表/字段/指标/取值）。"

    return res


def expand_tables(res: Resolution, onto: Ontology) -> list[str]:
    """把指标依赖字段、取值所属列反推回表，保证 JOIN 路径完整"""
    tables = set(res.tables)
    for col_id in res.columns:
        tables.add(col_id.split(".")[0])
    for name in res.metrics:
        for dep in onto.metrics[name].depends_on:
            if dep not in res.columns:
                res.columns.append(dep)
            tables.add(dep.split(".")[0])
    for _, col_id in res.values:
        tables.add(col_id.split(".")[0])
    # 问"多少单/几笔"必须从事实表计数，否则会被算成"有多少个会员"
    if res.count_intent and any(k in res.question for k in ("单", "订单", "笔")):
        tables.add("fact_order")
    if "fact_order" in tables:
        tables.add("fact_order")
    res.tables = sorted(tables)

    # 兜底：识别出实体/时间但没有任何度量词时，按主指标 GMV 处理
    # （"一季度总共卖了多少"这类问法，口径默认销售额；计数类问题不兜底）
    has_measure_col = any(onto.columns[c].is_measure for c in res.columns if c in onto.columns)
    if not res.metrics and not has_measure_col and not res.count_intent and "fact_order" in tables:
        res.metrics.append("GMV")
        for dep in onto.metrics["GMV"].depends_on:
            if dep not in res.columns:
                res.columns.append(dep)
        res.matched_by["<默认指标>"] = "metric"
    return res.tables
