"""跨项目对比：对同一份评测集的两个 run 重算「严格口径」指标

为什么需要"严格口径"：
  原项目的 eval 里，`handled` 只检查"结果里有没有数字"。于是出现这种情况——
  问"现在仓库爆仓了几个"，它生成了一条 SQL，返回一列叫「爆仓仓库数」、值为 NULL，
  就被记为"正确处理"。实际上它编了一个不存在的字段，属于幻觉，不是拒绝。
  本脚本用严格口径重算：**生成了 SQL 并返回结果集（哪怕值是 NULL）都不算拒绝**。

用法：
  python scripts/compare_runs.py --base <A.jsonl> --treat <B.jsonl> --label-a RAG --label-b OAG
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def load(path: str | Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def is_refused_strict(row: dict) -> bool:
    """没生成 SQL，或结果集为空，才算真拒绝；返回一行 NULL 也算"假装回答" """
    if not row.get("sql"):
        return True
    return not row.get("pred_result")


def stats(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["answerable"]]
    un = [r for r in rows if not r["answerable"]]
    ex = sum(1 for r in ans if r["ex"])
    rej = sum(1 for r in un if is_refused_strict(r))
    lat = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    calls = [r["llm_calls"] for r in rows if r.get("llm_calls") is not None]
    cats = {}
    for r in ans:
        c = cats.setdefault(r["category"], [0, 0])
        c[1] += 1
        c[0] += 1 if r["ex"] else 0
    return {
        "n_ans": len(ans),
        "n_un": len(un),
        "ex": ex,
        "ex_rate": ex / max(len(ans), 1),
        "ex_ci": wilson(ex, len(ans)),
        "refuse": rej,
        "refuse_rate": rej / max(len(un), 1),
        "p50": p50,
        "p95": p95,
        "calls": (sum(calls) / len(calls)) if calls else None,
        "cats": {k: (v[0], v[1]) for k, v in cats.items()},
    }


def mcnemar(base: list[dict], treat: list[dict]) -> dict:
    b = {r["id"]: r for r in base}
    t = {r["id"]: r for r in treat}
    ids = [i for i in b if i in t and b[i]["ex"] is not None and t[i]["ex"] is not None]
    fixed = [i for i in ids if b[i]["ex"] is False and t[i]["ex"] is True]
    broke = [i for i in ids if b[i]["ex"] is True and t[i]["ex"] is False]
    n = len(fixed) + len(broke)
    k = min(len(fixed), len(broke))
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
    return {"n": len(ids), "fixed": fixed, "broke": broke, "p": p}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--treat", required=True)
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    args = ap.parse_args()

    a, b = load(args.base), load(args.treat)
    sa, sb = stats(a), stats(b)

    print(f"\n{'指标':<22}{args.label_a:>16}{args.label_b:>16}{'Δ':>12}")
    print("-" * 68)

    def line(name, va, vb, fmt, delta=True, unit=""):
        d = ""
        if delta and isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            d = f"{vb - va:+.{fmt}f}{unit}"
        print(f"{name:<22}{va:>{16}.{fmt}f}{vb:>{16}.{fmt}f}{d:>12}")

    print(f"{'可答题数':<22}{sa['n_ans']:>16}{sb['n_ans']:>16}{'':>12}")
    line("EX 准确率", sa["ex_rate"], sb["ex_rate"], 4)
    ci_a = f"[{sa['ex_ci'][0]:.2f}, {sa['ex_ci'][1]:.2f}]"
    ci_b = f"[{sb['ex_ci'][0]:.2f}, {sb['ex_ci'][1]:.2f}]"
    print(f"{'EX 95%CI':<22}{ci_a:>16}{ci_b:>16}{'':>12}")
    print(f"{'答对数':<22}{sa['ex']:>16}{sb['ex']:>16}{sb['ex'] - sa['ex']:>+12}")
    print(f"{'超纲题数':<22}{sa['n_un']:>16}{sb['n_un']:>16}{'':>12}")
    line("严格拒绝率", sa["refuse_rate"], sb["refuse_rate"], 4)
    print(f"{'严格拒绝数':<22}{sa['refuse']:>16}{sb['refuse']:>16}{sb['refuse'] - sa['refuse']:>+12}")
    print(f"{'P50 延迟 ms':<22}{sa['p50']:>16.0f}{sb['p50']:>16.0f}{sb['p50'] - sa['p50']:>+12.0f}")
    print(f"{'P95 延迟 ms':<22}{sa['p95']:>16.0f}{sb['p95']:>16.0f}{sb['p95'] - sa['p95']:>+12.0f}")
    if sb["calls"] is not None:
        print(f"{'平均 LLM 调用/题':<22}{'—':>16}{sb['calls']:>16.2f}{'':>12}")

    print("\n分类 EX：")
    cats = sorted(set(sa["cats"]) | set(sb["cats"]))
    for c in cats:
        ka, na = sa["cats"].get(c, (0, 0))
        kb, nb = sb["cats"].get(c, (0, 0))
        print(f"  {c:<24}{f'{ka}/{na}':>10}{f'{kb}/{nb}':>12}")

    m = mcnemar(a, b)
    print(f"\n配对检验（n={m['n']}）: 修复 {len(m['fixed'])} {m['fixed']} | 退化 {len(m['broke'])} {m['broke']}")
    print(f"McNemar 精确检验 p = {m['p']:.4f}")

    print("\n严格口径下，基线被判为『假装回答』的超纲题：")
    for r in a:
        if not r["answerable"] and not is_refused_strict(r):
            print(f"  [{r['id']}] {r['question']}")
            print(f"        返回: {str(r['pred_result'])[:90]}")


if __name__ == "__main__":
    main()
