"""汇总 data/bench/results/<label>.jsonl：召回、噪声、耗时。用法：python summary.py <label>"""
import json, sys
from collections import defaultdict
from pathlib import Path

rows = [json.loads(l) for l in (Path(__file__).resolve().parent.parent.parent / "data/bench/results" / f"{sys.argv[1]}.jsonl").read_text().splitlines()]
by = defaultdict(list)
for r in rows:
    by[r["source"]].append(r)
    by["ALL"].append(r)
print(f"{'来源':9} {'用例':>4} {'位置命中':>8} {'语义命中':>8} {'平均发现':>8} {'平均耗时s':>9} {'报错':>4}")
for k in ("goreal", "advisory", "mined", "ALL"):
    g = by.get(k, [])
    if g:
        print(f"{k:9} {len(g):>4} {sum(r['loc_hit'] for r in g):>8} {sum(r['judge_hit'] for r in g):>8} "
              f"{sum(r['n'] for r in g) / len(g):>8.1f} {sum(r['secs'] for r in g) / len(g):>9.0f} {sum(1 for r in g if r['error']):>4}")
print("\n逐用例（★=语义命中 ◆=仅位置命中 ✗=未命中）")
for r in rows:
    mark = "★" if r["judge_hit"] else "◆" if r["loc_hit"] else "✗"
    print(f"{mark} {r['id']:28} n={r['n']:<2} {r['secs']:>4}s" + (f"  ERR {r['error'][:60]}" if r["error"] else ""))
