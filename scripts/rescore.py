"""结果集比对（修正版）+ 历史 run 重算

修正了什么：
  旧实现在排序时用 `str(set(...))` 当 key，而 set 的元素顺序不稳定；
  pred 只要多带一个字符串列（例如多返回 product_id），排序键就和 gold 不同，
  于是 zip 时行被错配，本来正确的结果被判错（E045 就是这样被冤枉的）。
  新实现改成「贪心一一匹配」，不依赖行顺序，也允许 pred 多带描述列。

用法：
  python scripts/rescore.py --run <run.jsonl> [--run ...]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _numerics(row: dict) -> list[float]:
    return sorted(
        float(v)
        for v in row.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )


def _strings(row: dict) -> set[str]:
    return {str(v) for v in row.values() if isinstance(v, str)}


def _row_match(gold: dict, pred: dict, tol: float = 0.01) -> bool:
    gn, pn = _numerics(gold), _numerics(pred)
    if gn:
        if len(gn) != len(pn):
            return False
        for a, b in zip(gn, pn):
            if abs(a - b) > max(tol, abs(a) * 0.001):
                return False
    return _strings(gold).issubset(_strings(pred))


def results_equal(gold_rows, pred_rows, tol: float = 0.01) -> bool:
    """行数一致 + 每行能在对方找到唯一匹配；不依赖行顺序"""
    if gold_rows is None or pred_rows is None:
        return False
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    pool = list(range(len(pred_rows)))
    for g in gold_rows:
        hit = next((i for i in pool if _row_match(g, pred_rows[i], tol)), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


def rescore(rows: list[dict]) -> dict:
    fixed, broke = [], []
    for r in rows:
        if r["answerable"]:
            new = results_equal(
                r["gold_result"], r["pred_result"], r.get("result_tolerance") or 0.01
            )
            r["ex_old"] = r["ex"]
            r["ex"] = bool(new)
            if new and r["ex_old"] is False:
                fixed.append(r["id"])
            if not new and r["ex_old"] is True:
                broke.append(r["id"])
    return {"fixed": fixed, "broke": broke}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, help="jsonl 路径，可多次指定")
    args = ap.parse_args()

    for path in args.run:
        p = Path(path)
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        old = sum(1 for r in rows if r.get("ex") is True)
        n_ans = sum(1 for r in rows if r["answerable"])
        diff = rescore(rows)
        new = sum(1 for r in rows if r.get("ex") is True)
        print(f"\n{p.name}")
        print(f"  可答题 {n_ans}｜修正前 {old}/{n_ans} = {old/n_ans:.4f}｜修正后 {new}/{n_ans} = {new/n_ans:.4f}")
        if diff["fixed"]:
            print(f"  被判错→改判为对: {diff['fixed']}")
        if diff["broke"]:
            print(f"  被判对→改判为错: {diff['broke']}")

        out = p.with_name(p.stem + ".rescored.jsonl")
        out.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )
        print(f"  已写入 {out.name}")


if __name__ == "__main__":
    main()
