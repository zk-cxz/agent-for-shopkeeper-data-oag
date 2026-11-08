"""解析器体检：把整个评测集过一遍本体解析，检查误拒 / 漏拒

不调大模型、不连数仓，秒级输出。用来在跑完整评测之前先定位解析层问题。
用法：python scripts/diagnose_resolver.py [--show-fp] [--show-fn]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ontology.loader import load_ontology  # noqa: E402
from app.ontology.resolver import _build_patterns, resolve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "eval" / "data" / "eval-v3.0.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-fp", action="store_true", help="显示误拒的可答题")
    ap.add_argument("--show-fn", action="store_true", help="显示漏拒的超纲题")
    args = ap.parse_args()

    onto = load_ontology()
    pats = _build_patterns(onto)
    items = [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()]

    fp, fn = [], []
    for it in items:
        res = resolve(it["question"], onto, pats)
        if it["answerable"] and res.refused:
            fp.append((it, res))
        if not it["answerable"] and not res.refused:
            fn.append((it, res))

    ans = [i for i in items if i["answerable"]]
    unans = [i for i in items if not i["answerable"]]
    print(f"题目总数 {len(items)} | 可答 {len(ans)} | 超纲 {len(unans)}")
    print(f"误拒（可答被拒）: {len(fp)} / {len(ans)} = {len(fp)/max(len(ans),1):.1%}")
    print(f"漏拒（超纲未拒）: {len(fn)} / {len(unans)} = {len(fn)/max(len(unans),1):.1%}")

    if args.show_fp:
        print("\n--- 误拒明细 ---")
        for it, res in fp:
            print(f"[{it['id']}] {it['question']}")
            print(f"      未登记={res.unresolved} 指标={res.metrics} 列={res.columns} 实体={res.tables}")
    if args.show_fn:
        print("\n--- 漏拒明细 ---")
        for it, res in fn:
            print(f"[{it['id']}] {it['question']}")
            print(f"      命中 指标={res.metrics} 列={res.columns} 取值={[v for v,_ in res.values]} 实体={res.tables}")


if __name__ == "__main__":
    main()
