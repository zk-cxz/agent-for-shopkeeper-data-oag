"""
评测跑分脚本（进程内执行，直接拿到每个节点的中间结果）

为什么在进程内跑而不是打 HTTP：
  1. 可以拿到 retrieved_column_infos / table_infos 等中间状态，才能算检索指标；
  2. 不受 SSE 序列化影响，便于精确计时；
  3. 少一层网络，延迟更接近模型本身的耗时。

用法：
  # 基线（dev 集）
  python eval/scripts/run_eval.py --run-id E0-baseline
  # 只跑检索层（不跑完整图，速度快）
  python eval/scripts/run_eval.py --run-id quick --limit 5

注意：
  - Qdrant 本地模式是文件独占锁，跑之前必须停掉后端；
  - 脚本会记录 RETRIEVAL_MODE / PROMPT_VARIANT / MAX_CORRECT_RETRY，便于复现。
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

# 让脚本能 import 项目内的 app.* 包（脚本位于 eval/scripts/ 下）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.pipeline.run import get_pipeline  # noqa: E402
from app.storage.dw import dw  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "eval" / "data" / "eval-v3.0.jsonl"
RESULTS = ROOT / "eval" / "results"


# ----------------------------------------------------------------------------
# 指标计算
# ----------------------------------------------------------------------------
def recall_at_k(gold: list[str], pred: list[str], k: int) -> float:
    if not gold:
        return 1.0
    hit = len(set(gold) & set(pred[:k]))
    return hit / len(gold)


def value_hit(gold_values: list[str], pred_values: list[str]) -> float:
    if not gold_values:
        return 1.0
    return len(set(gold_values) & set(pred_values)) / len(gold_values)


def _json_default(obj):
    """Decimal / date 等类型降级，保证结果能写入 jsonl"""
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def _flatten_non_numeric(rows):
    """把每行的非数值字段拼成字符串集合，用于宽松比对"""
    out = []
    for row in rows:
        strvals = sorted(str(v) for v in row.values() if isinstance(v, str))
        out.append(tuple(strvals))
    return sorted(out)


def _numeric_list(row):
    """提取行内数值（含 MySQL 返回的 Decimal），用于结果比对"""
    out = []
    for v in row.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, Decimal):
            out.append(float(v))
    return sorted(out)


def results_equal(gold_rows, pred_rows, tol: float = 0.01) -> bool:
    """结果集等价判定（贪心一一匹配，不依赖行顺序）

    修正了旧实现的一个坑：旧版用 str(set(...)) 排序，只要 pred 多带一个字符串列
    （例如多返回 product_id），排序键就和 gold 不同，zip 时行被错配，正确结果被判错。
    """
    if gold_rows is None or pred_rows is None:
        return False
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True

    def ok(g, pr):
        gn, pn = sorted(_numeric_list(g)), sorted(_numeric_list(pr))
        if gn:
            if len(gn) != len(pn):
                return False
            for a, b in zip(gn, pn):
                if abs(a - b) > max(tol, abs(a) * 0.001):
                    return False
        gs = {str(v) for v in g.values() if isinstance(v, str)}
        ps = {str(v) for v in pr.values() if isinstance(v, str)}
        return gs.issubset(ps)

    pool = list(range(len(pred_rows)))
    for g in gold_rows:
        hit = next((k for k in pool if ok(g, pred_rows[k])), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


def wilson_ci(k: int, n: int, z: float = 1.96):
    """二项比例的 Wilson 置信区间，小样本比正态近似可靠"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ----------------------------------------------------------------------------
# 单题执行
# ----------------------------------------------------------------------------
async def run_one(item, pipeline):
    """单题执行：直接调用 OAG 管道，采样本体解析结果与最终结果集"""
    t0 = time.perf_counter()
    res = await pipeline.run(item["question"])
    latency_ms = res.latency_ms or (time.perf_counter() - t0) * 1000

    # 只有"根本没跑出 SQL"才算 None；正常执行但结果为空集，要保留 [] 才能和空 gold 比对
    result_rows = res.rows if res.sql is not None else None
    error_msg = res.message if (res.refused or res.message) else None
    sql = res.sql

    pred_columns = list(res.resolution.columns)
    pred_metrics = list(res.resolution.metrics)
    pred_values = [v for v, _ in res.resolution.values]
    pred_tables = list(res.resolution.tables)

    rec = {
        "recall5": recall_at_k(item["gold_columns"], pred_columns, 5),
        "recall10": recall_at_k(item["gold_columns"], pred_columns, 10),
        "recall20": recall_at_k(item["gold_columns"], pred_columns, 20),
        "all_hit": 1.0 if set(item["gold_columns"]).issubset(set(pred_columns)) else 0.0,
        "metric_recall3": recall_at_k(item["gold_metrics"], pred_metrics, 3),
        "value_hit": value_hit(item["gold_values"], pred_values),
        "table_hit": 1.0
        if all(c.split(".")[0] in pred_tables for c in item["gold_columns"])
        else 0.0,
    }

    valid = None
    if sql and item["answerable"]:
        try:
            await dw.validate(sql)
            valid = True
        except Exception:  # noqa: BLE001
            valid = False

    ex = None
    if item["answerable"]:
        ex = bool(valid) and results_equal(
            item["gold_result"], result_rows, item.get("result_tolerance") or 0.01
        )

    handled = None
    if not item["answerable"]:
        if error_msg or not result_rows:
            handled = True
        else:
            handled = not any(
                isinstance(v, (int, float))
                for row in result_rows
                for v in (row.values() if isinstance(row, dict) else [])
            )

    error_type = None
    if item["answerable"]:
        if not sql:
            error_type = "未生成SQL"
        elif valid is False:
            error_type = "SQL无效"
        elif ex is False:
            error_type = "结果错"

    return {
        "id": item["id"],
        "category": item["category"],
        "split": item["split"],
        "question": item["question"],
        "answerable": item["answerable"],
        "refused": bool(res.refused),
        "refuse_reason": res.resolution.refuse_reason if res.refused else None,
        "unresolved": list(res.resolution.unresolved),
        "llm_calls": res.llm_calls,
        "pred_columns": pred_columns,
        "pred_metrics": pred_metrics,
        "pred_values": pred_values,
        "pred_tables": pred_tables,
        "sql": sql,
        "valid": valid,
        "pred_result": result_rows,
        "gold_result": item["gold_result"],
        "ex": ex,
        "handled": handled,
        "error_type": error_type,
        "error_msg": error_msg,
        "latency_ms": round(latency_ms, 1),
        "ttfb_ms": None,
        **rec,
    }


# ----------------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------------
def summarize(rows, run_id: str, dataset_ver: str):
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]

    def mean(key, subset=None):
        vals = [r[key] for r in (subset if subset is not None else rows) if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    n = len(answerable)
    ex_k = sum(1 for r in answerable if r["ex"])
    valid_k = sum(1 for r in answerable if r["valid"])
    handled_k = sum(1 for r in unanswerable if r["handled"])

    lat = sorted(r["latency_ms"] for r in rows)
    def pct(p):
        if not lat:
            return None
        idx = min(len(lat) - 1, int(round((len(lat) - 1) * p)))
        return round(lat[idx], 1)

    by_cat = {}
    for cat in sorted({r["category"] for r in rows}):
        sub = [r for r in rows if r["category"] == cat]
        sub_ans = [r for r in sub if r["answerable"]]
        by_cat[cat] = {
            "n": len(sub),
            "EX": round(sum(1 for r in sub_ans if r["ex"]) / len(sub_ans), 4) if sub_ans else None,
            "Recall10": round(
                sum(r["recall10"] for r in sub_ans) / len(sub_ans), 4
            ) if sub_ans else None,
        }

    return {
        "run_id": run_id,
        "dataset": dataset_ver,
        "n_total": len(rows),
        "n_answerable": n,
        "n_unanswerable": len(unanswerable),
        "env": {
            "RETRIEVAL_MODE": os.getenv("RETRIEVAL_MODE", "qdrant"),
            "PROMPT_VARIANT": os.getenv("PROMPT_VARIANT", ""),
            "MAX_CORRECT_RETRY": os.getenv("MAX_CORRECT_RETRY", "0"),
        },
        "retrieval": {
            "Recall@5": mean("recall5", answerable),
            "Recall@10": mean("recall10", answerable),
            "Recall@20": mean("recall20", answerable),
            "All-Hit": mean("all_hit", answerable),
            "MetricRecall@3": mean("metric_recall3", answerable),
            "ValueHit": mean("value_hit", answerable),
            "TableHit": mean("table_hit", answerable),
        },
        "generation": {
            "ValidRate": round(valid_k / n, 4) if n else None,
            "EX": round(ex_k / n, 4) if n else None,
            "EX_ci95": [round(x, 4) for x in wilson_ci(ex_k, n)] if n else None,
            "EX_count": f"{ex_k}/{n}",
            "UnanswerableHandled": f"{handled_k}/{len(unanswerable)}",
        },
        "latency": {"P50_ms": pct(0.5), "P95_ms": pct(0.95), "TTFB_P50_ms": None},
        "by_category": by_cat,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条，用于冒烟")
    ap.add_argument("--dataset", default=str(DATA))
    args = ap.parse_args()

    items = [json.loads(line) for line in Path(args.dataset).open(encoding="utf-8") if line.strip()]
    if args.split != "all":
        items = [i for i in items if i["split"] == args.split]
    if args.limit:
        items = items[: args.limit]

    pipeline = get_pipeline()

    rows = []
    for idx, item in enumerate(items, 1):
        print(f"[{idx}/{len(items)}] {item['id']} {item['question']}", flush=True)
        row = await run_one(item, pipeline)
        print(
            f"    EX={row['ex']} valid={row['valid']} refused={row['refused']} "
            f"llm={row['llm_calls']} {int(row['latency_ms'])}ms",
            flush=True,
        )
        rows.append(row)

    await dw.close()

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.run_id}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            # MySQL 的 SUM/AVG 会返回 Decimal，json 默认不认，这里统一降级为可序列化类型
            f.write(
                json.dumps(row, ensure_ascii=False, default=_json_default) + "\n"
            )

    summary = summarize(rows, args.run_id, Path(args.dataset).stem)
    (RESULTS / f"{args.run_id}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
