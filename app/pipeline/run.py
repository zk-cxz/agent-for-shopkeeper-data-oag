"""OAG 主链路

    问题 → 本体解析（0 次 LLM，确定性） → 组装上下文（0 次）
         → 生成 SQL（1 次 LLM） → 规则校验（0 次） → [违规则修一次，最多 1 次]
         → EXPLAIN 校验 → 执行

对比原 RAG 版：6 次串行 LLM 调用（3 个召回扩展 + 2 个过滤 + 1 个生成）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from openai import AsyncOpenAI

from app.config import app_config
from app.log import logger
from app.ontology.loader import load_ontology
from app.ontology.model import Ontology
from app.ontology.resolver import Resolution, expand_tables, resolve, _build_patterns
from app.ontology.sqlcheck import check_sql, clean_sql
from app.storage.dw import dw

ROOT = Path(__file__).resolve().parents[2]
MAX_VALUES_PER_COLUMN = 20


@dataclass
class QueryResult:
    question: str
    resolution: Resolution
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False
    message: str | None = None
    latency_ms: float = 0.0
    llm_calls: int = 0
    violations: list[str] = field(default_factory=list)


def _load_prompt(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.prompt").read_text(encoding="utf-8")


def build_context(res: Resolution, onto: Ontology) -> str:
    """把本体裁成"只跟这个问题有关"的一小块结构化上下文"""
    involved = set(res.tables) or {"fact_order"}
    dr = onto.data_range or {}

    ctx: dict[str, Any] = {
        "数据范围": {
            "起": dr.get("min_date"),
            "止": dr.get("max_date"),
            "说明": dr.get("description"),
        },
        "对象": {},
        "指标": {},
        "取值过滤": [],
        "关联关系": [],
        "约束": [],
    }

    for table in sorted(involved):
        t = onto.tables.get(table)
        if not t:
            continue
        cols: dict[str, Any] = {}
        for col in onto.columns_of(table):
            item: dict[str, Any] = {"类型": col.dtype, "角色": col.role}
            if col.description:
                item["说明"] = col.description
            if col.is_measure:
                item["可用聚合"] = ["SUM", "AVG"]
            if col.values:
                shown = col.values[:MAX_VALUES_PER_COLUMN]
                item["取值"] = shown + (["…"] if len(col.values) > len(shown) else [])
            cols[col.name] = item
        ctx["对象"][table] = {
            "类型": t.kind,
            "粒度": t.grain,
            "说明": t.description,
            "字段": cols,
        }

    for name in res.metrics:
        m = onto.metrics[name]
        ctx["指标"][str(name)] = {
            "展示名": m.display,
            "口径": m.formula,
            "说明": m.description,
            "依赖字段": m.depends_on,
        }

    for value, col_id in res.values:
        ctx["取值过滤"].append({"字段": col_id, "值": value})

    for fk in onto.joins_between(involved):
        ctx["关联关系"].append(f"{fk.from_column} = {fk.to_column}")

    if str(res.time_label or "").startswith("Q"):
        ctx["时间限定"] = f"季度 = {res.time_label}（{dr.get('min_date')} 所在年度）"
    if res.count_intent and any(k in res.question for k in ("单", "订单", "笔")):
        ctx["计数对象"] = (
            "fact_order.order_id —— 问的是单量，必须 COUNT 事实表的订单，"
            "维度表（客户/商品/地区）只用于 WHERE 过滤"
        )
    if res.time_grain:
        col = (onto.temporal.get("grain_columns") or {}).get(res.time_grain)
        ctx["统计粒度"] = f"按{res.time_grain}（{col}）"

    # 约束来自本体 rules，是"代码会检查"的硬约束，不是口头建议
    ctx["约束"] = [
        "度量字段必须聚合",
        "GROUP BY 必须与非聚合输出列一致，且不得包含 SELECT 之外的列",
        "业务取值必须出现在 WHERE 中",
        "时间粒度通过 dim_date 的 year/quarter/month/day 表达，quarter 为 'Q1' 字符串",
        f"禁止使用超出 {dr.get('min_date')}~{dr.get('max_date')} 的日期",
    ]

    return yaml.dump(ctx, allow_unicode=True, sort_keys=False)


class OAGPipeline:
    def __init__(self) -> None:
        self.onto = load_ontology(str(ROOT / str(app_config.ontology.json_path)))
        self.patterns = _build_patterns(self.onto)
        self.client = AsyncOpenAI(
            api_key=str(app_config.llm.api_key),
            base_url=str(app_config.llm.base_url),
            timeout=90.0,
        )
        self.model = str(app_config.llm.model_name)

    async def _llm(self, prompt: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            temperature=float(app_config.llm.temperature),
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.choices[0].message.content or "").strip()

    async def run(
        self,
        question: str,
        emit: Callable[[dict], None] | None = None,
    ) -> QueryResult:
        emit = emit or (lambda e: None)
        t0 = time.perf_counter()
        result = QueryResult(question=question, resolution=Resolution(question=question))

        # ---- 1. 本体解析（确定性，0 次 LLM）----
        emit({"type": "progress", "step": "本体解析", "status": "running"})
        res = resolve(question, self.onto, self.patterns)
        result.resolution = res
        emit({"type": "progress", "step": "本体解析", "status": "success", "detail": res.summary()})
        if res.refused:
            result.refused = True
            result.message = res.refuse_reason
            emit(
                {
                    "type": "progress",
                    "step": "拒绝作答",
                    "status": "error",
                    "detail": res.refuse_reason,
                }
            )
            result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.info(f"拒绝作答：{res.refuse_reason}")
            return result

        # ---- 2. 组装上下文（只取相关对象，0 次 LLM）----
        emit({"type": "progress", "step": "组装上下文", "status": "running"})
        expand_tables(res, self.onto)
        context = build_context(res, self.onto)
        logger.debug(f"本体上下文：\n{context}")
        emit(
            {
                "type": "progress",
                "step": "组装上下文",
                "status": "success",
                "detail": f"{len(res.tables)} 个对象 / {len(res.columns)} 个属性",
            }
        )

        # ---- 3. 生成 SQL（唯一/主要的 LLM 调用）----
        emit({"type": "progress", "step": "生成SQL", "status": "running"})
        sql = clean_sql(
            await self._llm(
                _load_prompt("generate_sql").format(context=context, question=question)
            )
        )
        result.llm_calls += 1
        emit({"type": "progress", "step": "生成SQL", "status": "success", "detail": sql})

        # ---- 4. 规则校验 + 最多修一次 ----
        emit({"type": "progress", "step": "规则校验", "status": "running"})
        violations = check_sql(sql, res, self.onto)
        if violations:
            result.violations = violations
            emit(
                {
                    "type": "progress",
                    "step": "规则校验",
                    "status": "error",
                    "detail": "；".join(violations),
                }
            )
            emit({"type": "progress", "step": "修正SQL", "status": "running"})
            sql = clean_sql(
                await self._llm(
                    _load_prompt("repair_sql").format(
                        context=context,
                        question=question,
                        sql=sql,
                        violations="\n".join(f"- {v}" for v in violations),
                    )
                )
            )
            result.llm_calls += 1
            result.violations = check_sql(sql, res, self.onto)
            emit({"type": "progress", "step": "修正SQL", "status": "success", "detail": sql})
        else:
            emit({"type": "progress", "step": "规则校验", "status": "success", "detail": "通过"})

        # ---- 5. EXPLAIN 校验（数据库当裁判）----
        emit({"type": "progress", "step": "执行SQL", "status": "running"})
        try:
            await dw.validate(sql)
        except Exception as exc:  # noqa: BLE001
            result.message = f"SQL 校验失败：{exc}"
            emit({"type": "progress", "step": "执行SQL", "status": "error", "detail": str(exc)})
            result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.warning(result.message)
            return result

        # ---- 6. 执行 ----
        try:
            rows = await dw.fetch(sql)
        except Exception as exc:  # noqa: BLE001
            result.message = f"SQL 执行失败：{exc}"
            emit({"type": "progress", "step": "执行SQL", "status": "error", "detail": str(exc)})
            result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            return result

        result.sql = sql
        result.rows = rows
        result.columns = list(rows[0].keys()) if rows else []
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        emit({"type": "progress", "step": "执行SQL", "status": "success", "detail": f"{len(rows)} 行"})
        return result


pipeline: OAGPipeline | None = None


def get_pipeline() -> OAGPipeline:
    global pipeline
    if pipeline is None:
        pipeline = OAGPipeline()
    return pipeline
