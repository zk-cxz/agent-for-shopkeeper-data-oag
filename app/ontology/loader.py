"""本体加载：读编译产物 ontology.json，构造 O(1) 命中索引

注意：运行期不连元数据库、不连向量库。所有对象与值域都在 JSON 里，
这正是 OAG 相对 RAG 的工程收益之一 —— 依赖从 5 个降到 2 个（MySQL + LLM）。
"""

import json
from pathlib import Path

from app.ontology.model import (
    Column,
    ForeignKey,
    Metric,
    Ontology,
    Table,
)

ROOT = Path(__file__).resolve().parents[2]


def _build_indexes(onto: Ontology) -> None:
    lex = onto.lexicon or {}

    # 实体词 -> 表（"订单"->fact_order）
    for alias, table in (lex.get("entity_aliases") or {}).items():
        onto.entity_aliases[alias] = table
    for name, t in onto.tables.items():
        onto.entity_aliases.setdefault(name, name)

    # 指标别名 -> 指标名
    for name, m in onto.metrics.items():
        onto.metric_index[name] = name
        onto.metric_index[m.display] = name
        for a in m.aliases:
            onto.metric_index[a] = name

    # 列别名 -> 列 id
    for col in onto.columns.values():
        onto.column_index[col.id] = col.id
        onto.column_index[col.name] = col.id
        for a in col.aliases:
            onto.column_index.setdefault(a, col.id)

    # 取值 -> (值, 所属列)；同时展开后缀变体（华北 -> 华北地区）
    suffixes = lex.get("value_suffixes") or []
    for col in onto.columns.values():
        for v in col.values:
            onto.value_index.setdefault(v, []).append((v, col.id))
            for s in suffixes:
                onto.value_index.setdefault(f"{v}{s}", []).append((v, col.id))

    # 时间表达 -> 标签
    for expr, label in (lex.get("time_expressions") or {}).items():
        onto.time_index[expr] = label


def load_ontology(path: str | Path | None = None) -> Ontology:
    p = Path(path) if path else ROOT / "conf" / "ontology.json"
    raw = json.loads(Path(p).read_text(encoding="utf-8"))

    onto = Ontology(
        version=raw.get("version", "1.0"),
        domain=raw.get("domain", ""),
        tables={
            k: Table(
                name=k,
                kind=v.get("kind", "dimension"),
                grain=v.get("grain", ""),
                description=v.get("description", ""),
                aliases=list(v.get("aliases") or []),
            )
            for k, v in raw["tables"].items()
        },
        columns={
            k: Column(
                id=k,
                table=v["table"],
                name=v["name"],
                role=v.get("role", "dimension"),
                dtype=v.get("dtype", ""),
                description=v.get("description", ""),
                is_measure=bool(v.get("is_measure")),
                is_temporal=bool(v.get("is_temporal")),
                aliases=list(v.get("aliases") or []),
                values=list(v.get("values") or []),
            )
            for k, v in raw["columns"].items()
        },
        metrics={
            k: Metric(
                name=k,
                display=v.get("display", k),
                formula=v.get("formula", ""),
                description=v.get("description", ""),
                depends_on=list(v.get("depends_on") or []),
                aliases=list(v.get("aliases") or []),
            )
            for k, v in raw["metrics"].items()
        },
        foreign_keys=[
            ForeignKey(from_column=f["from"], to_column=f["to"])
            for f in raw.get("foreign_keys", [])
        ],
        rules=raw.get("rules", {}),
        lexicon=raw.get("lexicon", {}),
        data_range=raw.get("data_range", {}),
        temporal=raw.get("temporal", {}),
    )
    _build_indexes(onto)
    return onto
