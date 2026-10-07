"""汇总 data/bench/results/<label>.jsonl：召回、噪声、耗时。用法：python summary.py <label>"""
import json, sys
from collections import defaultdict
from pathlib import Path

rows = [json.loads(l) for l in (Path(__file__).resolve().parent.parent.parent / "data/bench/results" / f"{sys.argv[1]}.jsonl").read_text().splitlines()]
if rows and rows[0].get("mode") == "fwd":  # 负例：审查的是“修复提交”本身，发现都需要人工核对
    n, fp = len(rows), [r for r in rows if r["n"]]
    fs = [(r, f) for r in rows for f in r["findings"]]
    print(f"负例 {n} 个（审查修复提交本身，理想情况下无发现）")
    print(f"有发现的用例 {len(fp)}/{n}（{len(fp) / max(n, 1):.0%}）；发现共 {len(fs)} 条，其中 P0/P1 {sum(f['sev'] in ('P0', 'P1') for _, f in fs)} 条，"
          f"落在修复位置附近 {sum(f['loc_hit'] for _, f in fs)} 条；平均耗时 {sum(r['secs'] for r in rows) / max(n, 1):.0f}s；报错 {sum(1 for r in rows if r['error'])}")
    print("\n逐条发现（※ 需要人工判定：误报 / 修复本身确有问题）")
    for r, f in fs:
        print(f"※ {r['id']:30} {f['sev']} {f['cat']:12} {f['file']}:{f['line']}{'  [修复位置附近]' if f['loc_hit'] else ''}\n     {f['title']}")
    print("\n无发现的用例：" + ("、".join(r["id"] for r in rows if not r["n"]) or "（无）"))
    raise SystemExit
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
