"""本体数据类型：对象（表/列/指标/取值）+ 关系 + 规则

设计要点：这些结构全部来自编译产物 ontology.json，运行时不查元数据库，
也不用向量库 —— 所有可检索对象都在这一份文件里。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Column:
    """列 = 对象的一个属性"""

    id: str  # 规范标识，形如 fact_order.order_amount
    table: str
    name: str
    role: str  # primary_key / foreign_key / measure / dimension
    dtype: str
    description: str = ""
    is_measure: bool = False
    is_temporal: bool = False
    aliases: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)  # 值域（仅维度字段）


@dataclass
class Table:
    """表 = 对象类型"""

    name: str
    kind: str  # fact / dimension
    grain: str
    description: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class Metric:
    """指标 = 带口径的可计算对象"""

    name: str
    display: str
    formula: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    """关系边：JOIN 路径由本体给定"""

    from_column: str
    to_column: str


@dataclass
class Ontology:
    version: str
    domain: str
    tables: dict[str, Table]
    columns: dict[str, Column]
    metrics: dict[str, Metric]
    foreign_keys: list[ForeignKey]
    rules: dict[str, Any]
    lexicon: dict[str, Any]
    data_range: dict[str, Any]
    temporal: dict[str, Any]

    # ---- 派生索引：编译期构造，运行期 O(1) 命中 ----
    entity_aliases: dict[str, str] = field(default_factory=dict)  # 订单 -> fact_order
    metric_index: dict[str, str] = field(default_factory=dict)  # 客单价 -> AOV
    column_index: dict[str, str] = field(default_factory=dict)  # 营业额 -> order_amount
    value_index: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # 华北 -> [(华北, col_id)]
    time_index: dict[str, str] = field(default_factory=dict)  # 一季度 -> Q1

    def columns_of(self, table: str) -> list[Column]:
        return [c for c in self.columns.values() if c.table == table]

    def joins_between(self, tables: set[str]) -> list[ForeignKey]:
        """只返回涉及到的表之间的外键，避免把无关 JOIN 塞进上下文"""
        return [
            fk
            for fk in self.foreign_keys
            if fk.from_column.split(".")[0] in tables
            and fk.to_column.split(".")[0] in tables
        ]
