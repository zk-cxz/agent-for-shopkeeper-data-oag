"""本体编译：数据字典 + 人工语义层 → ontology.json

三步（对应真实 OAG 落地流程）：
  1. 从 meta 库拉"数据字典初稿"（表、字段、类型、角色、已登记别名）
  2. 从 dw 库回查真实取值，把维度字段的值域补进本体（否则"华北"无法锚定）
  3. 合并 conf/ontology.yaml 的人工语义（关系、规则、口径公式、业务词表补全）

产物是一份自包含的 JSON：运行期不再需要元数据库，也不再需要向量库。

用法：python scripts/build_ontology.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
META_DSN = "mysql+asyncmy://didilili:dili123@127.0.0.1:3306/meta?charset=utf8mb4"
DW_DSN = "mysql+asyncmy://didilili:dili123@127.0.0.1:3306/dw?charset=utf8mb4"
OUT = ROOT / "conf" / "ontology.json"

MAX_ENUM_CARDINALITY = 50  # 维度字段基数上限：超过这个数的字段不当枚举值域


async def _distinct_values(dw_conn, table: str, column: str) -> list[str]:
    sql = f"SELECT DISTINCT `{column}` AS v FROM `{table}` WHERE `{column}` IS NOT NULL LIMIT 201"
    rows = (await dw_conn.execute(text(sql))).scalars().fetchall()
    return [str(v) for v in rows]


async def build() -> None:
    semantic = yaml.safe_load((ROOT / "conf" / "ontology.yaml").read_text(encoding="utf-8"))
    lex = semantic.get("lexicon") or {}
    extra_col_aliases = lex.get("column_aliases") or {}
    extra_metric_aliases = lex.get("metric_aliases") or {}
    measures = set(semantic.get("measures") or [])
    value_exclude = set(lex.get("value_domain_exclude") or [])
    temporal_col = (semantic.get("temporal") or {}).get("column")

    meta_engine = create_async_engine(META_DSN)
    dw_engine = create_async_engine(DW_DSN)

    async with meta_engine.connect() as meta_conn, dw_engine.connect() as dw_conn:
        table_rows = (
            await meta_conn.execute(
                text("SELECT id, name, role, description FROM table_info ORDER BY id")
            )
        ).mappings().fetchall()
        column_rows = (
            await meta_conn.execute(
                text(
                    "SELECT id, name, type, role, examples, description, alias, table_id "
                    "FROM column_info ORDER BY table_id, id"
                )
            )
        ).mappings().fetchall()
        metric_rows = (
            await meta_conn.execute(
                text("SELECT id, name, description, relevant_columns, alias FROM metric_info")
            )
        ).mappings().fetchall()
        cm_rows = (
            await meta_conn.execute(text("SELECT column_id, metric_id FROM column_metric"))
        ).mappings().fetchall()

        # ---- 1. 对象类型 ----
        tables: dict[str, dict] = {}
        for r in table_rows:
            declared = semantic.get("tables", {}).get(r["id"], {})
            tables[r["id"]] = {
                "kind": declared.get("kind", "fact" if str(r["role"]).startswith("fact") else "dimension"),
                "grain": declared.get("grain", ""),
                "description": r["description"] or declared.get("description", ""),
                "aliases": list(declared.get("aliases") or []),
            }

        # ---- 2. 属性（字段）+ 值域 ----
        columns: dict[str, dict] = {}
        for r in column_rows:
            cid, table = r["id"], r["table_id"]
            name = r["name"]
            meta_alias = json.loads(r["alias"]) if r["alias"] else []
            aliases = list(dict.fromkeys(meta_alias + list(extra_col_aliases.get(cid) or [])))
            role = r["role"] or "dimension"
            is_measure = cid in measures or role == "measure"

            values: list[str] = []
            if (
                not is_measure
                and role == "dimension"
                and not name.endswith("_id")
                and cid not in value_exclude
            ):
                vals = await _distinct_values(dw_conn, table, name)
                if 0 < len(vals) <= MAX_ENUM_CARDINALITY:
                    values = vals

            columns[cid] = {
                "table": table,
                "name": name,
                "role": role,
                "dtype": r["type"] or "",
                "description": r["description"] or "",
                "is_measure": is_measure,
                "is_temporal": cid == temporal_col,
                "aliases": aliases,
                "values": values,
            }

    await meta_engine.dispose()
    await dw_engine.dispose()

    # ---- 3. 指标（口径以人工语义层为准，依赖关系来自 column_metric）----
    dep_map: dict[str, list[str]] = {}
    for r in cm_rows:
        dep_map.setdefault(r["metric_id"], []).append(r["column_id"])

    metrics: dict[str, dict] = {}
    for r in metric_rows:
        name = r["name"]
        declared = semantic.get("metrics", {}).get(name, {})
        meta_alias = json.loads(r["alias"]) if r["alias"] else []
        metrics[name] = {
            "display": declared.get("display", name),
            "formula": declared.get("formula", ""),
            "description": r["description"] or declared.get("description", ""),
            "depends_on": dep_map.get(r["id"], []),
            "aliases": list(
                dict.fromkeys(meta_alias + list(extra_metric_aliases.get(name) or []))
            ),
        }

    ontology = {
        "version": semantic.get("version", "1.0"),
        "domain": semantic.get("domain", ""),
        "data_range": semantic.get("data_range", {}),
        "tables": tables,
        "columns": columns,
        "metrics": metrics,
        "foreign_keys": semantic.get("foreign_keys", []),
        "temporal": semantic.get("temporal", {}),
        "rules": semantic.get("rules", {}),
        "lexicon": lex,
    }
    OUT.write_text(json.dumps(ontology, ensure_ascii=False, indent=2), encoding="utf-8")

    enum_cols = [c for c in columns.values() if c["values"]]
    print(f"[ok] 写入 {OUT}")
    print(f"     对象类型(table) : {len(tables)}")
    print(f"     属性(column)    : {len(columns)}")
    print(f"     指标(metric)    : {len(metrics)}")
    print(f"     关系(foreign_key): {len(ontology['foreign_keys'])}")
    print(f"     值域字段        : {len(enum_cols)} 个，取值合计 {sum(len(c['values']) for c in enum_cols)} 条")
    for c in [c for c in columns.values() if c["values"]]:
        print(f"       - {c['table']}.{c['name']}: {len(c['values'])} 个取值")


if __name__ == "__main__":
    asyncio.run(build())
