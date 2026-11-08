"""SQL 规则校验 —— 把"提示词里求模型自觉"变成"代码里强制执行"

原项目里这些约束写在提示词里，模型偶尔会违反（E038 的 GROUP BY 多带主键就是例子）。
在 OAG 里它们是本体 rules，生成后由本模块静态检查；违反就带着具体原因重生成一次。
"""

from __future__ import annotations

import re

from app.ontology.model import Ontology
from app.ontology.resolver import Resolution

_FENCE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")
_AGG = re.compile(r"\b(SUM|AVG|COUNT|MAX|MIN)\s*\(", re.I)
_SELECT_BLOCK = re.compile(r"select\s+(.*?)\s+from\s", re.I | re.S)
_GROUP_BY = re.compile(r"group\s+by\s+(.*?)(?:having|order\s+by|limit|$)", re.I | re.S)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)
_QUALIFIED = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")
_LONG_YEAR = re.compile(r"\b(20[2-9]\d[01]\d[0-3]\d)\b")


def clean_sql(text: str) -> str:
    s = (text or "").strip()
    s = _FENCE.sub("", s).strip()
    if s.lower().startswith("sql"):
        s = s[3:].strip()
    return s.rstrip(";").strip()


def _split_top_level(expr: str) -> list[str]:
    """按最外层逗号切分，忽略括号内的逗号"""
    parts, depth, buf = [], 0, []
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def check_sql(sql: str, res: Resolution, onto: Ontology) -> list[str]:
    """返回违反的规则列表；空列表表示通过"""
    problems: list[str] = []
    low = sql.lower()

    if not re.match(r"^\s*(select|with)\b", low):
        problems.append("只能生成 SELECT 查询，禁止任何写操作。")
        return problems

    # 1) 表 / 列必须在白名单内
    for tbl in _TABLE_REF.findall(sql):
        if tbl not in onto.tables:
            problems.append(f"表 `{tbl}` 不在本体中，禁止使用未登记的表。")
    for tbl, col in _QUALIFIED.findall(sql):
        if tbl in onto.tables and f"{tbl}.{col}" not in onto.columns:
            problems.append(f"字段 `{tbl}.{col}` 不在本体中，禁止编造字段。")

    select_block = _SELECT_BLOCK.search(sql)
    group_block = _GROUP_BY.search(sql)
    select_items = _split_top_level(select_block.group(1)) if select_block else []
    group_items = _split_top_level(group_block.group(1)) if group_block else []

    # 2) 度量必须被聚合包裹
    for item in select_items:
        for col in onto.columns.values():
            if col.is_measure and col.id in item.replace(" ", "") and not _AGG.search(item):
                problems.append(f"度量字段 `{col.id}` 必须用 SUM/AVG 等聚合函数包裹。")

    # 3) GROUP BY 完整性 + 不许多带隐藏列
    if group_items:
        normalized_group = [re.sub(r"\s+", "", g) for g in group_items]
        for item in select_items:
            expr = re.sub(r"\s+", "", item.split(" as ")[0].split(" AS ")[0])
            if _AGG.search(item) or not expr:
                continue
            # 允许 "a.b" 与 "b" 两种写法互相等价
            tail = expr.split(".")[-1]
            if expr not in normalized_group and not any(
                g.split(".")[-1] == tail for g in normalized_group
            ):
                problems.append(f"SELECT 中的非聚合列 `{expr}` 没有出现在 GROUP BY 中。")
        select_exprs = [re.sub(r"\s+", "", i.split(" as ")[0].split(" AS ")[0]) for i in select_items]
        for g in normalized_group:
            if g not in select_exprs and not any(g.split(".")[-1] == s.split(".")[-1] for s in select_exprs):
                problems.append(
                    f"GROUP BY 中的 `{g}` 不在 SELECT 输出列里，会把同一业务维度拆成多行，必须去掉。"
                )

    # 4) 问题里解析出的取值必须出现在过滤条件里
    if (onto.rules or {}).get("value_filter_required"):
        for value, col_id in res.values:
            if value not in sql:
                problems.append(f"问题限定了 `{col_id} = '{value}'`，但 SQL 里没有这个过滤条件。")

    # 5) 时间范围：不许用超出数据范围的年份
    for literal in _LONG_YEAR.findall(sql):
        year = int(literal[:4])
        max_date = int((onto.data_range or {}).get("max_date", 99999999))
        if int(literal) > max_date:
            problems.append(
                f"SQL 里出现 `{literal}`，超出数据范围 "
                f"{(onto.data_range or {}).get('min_date')}~{(onto.data_range or {}).get('max_date')}。"
            )

    # 6) 时间粒度必须走日期维度表
    if res.time_label and "dim_date" not in low and (onto.rules or {}).get("temporal_via_dimension"):
        problems.append("问题涉及季度/月份粒度，必须 JOIN dim_date 并使用维度列，不要从 date_id 硬算。")

    # 去重保序
    seen, out = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
