"""在 build.py 构造的用例上运行华佗并评分。
用法：python run.py <label> [--forward] [case_id ...]   结果写入 data/bench/results/<label>.jsonl（已有的用例会跳过，可断点续跑）
评分：位置命中 = 发现的文件相同且行号落在修复涉及的行范围 ±MARGIN 内；语义命中 = 模型判断该发现与已知缺陷描述是同一个问题。
注意：模型判分只用于初筛，试点阶段需要人工逐条核对。
--forward：负例模式。把 base/head 对调，审查的是“把 bug 修好”的那次真实提交（修复本身）。理想情况下不应报出问题，
报出的发现都要人工核对是误报还是修复本身确有瑕疵；用来估计误报率。结果的 id 带 @fwd 后缀。"""
import contextlib, io, json, logging, re, shutil, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path[:0] = [str(ROOT / "tests")]
logging.basicConfig(level=logging.WARNING)
import test_lifecycle as T  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from huatuo.deps import Deps  # noqa: E402
from huatuo.graph.build import build_graph  # noqa: E402
from huatuo.llm import human, invoke_structured  # noqa: E402
from huatuo.settings import Config, Env, ReviewConfig, Settings, StaticAnalysisConfig  # noqa: E402
from huatuo.store import Store  # noqa: E402

MARGIN = 12
BENCH = ROOT / "data" / "bench"


class Judge(BaseModel):
    same_defect: bool
    reason: str


def make_env(meta: dict):
    tmp = Path(tempfile.mkdtemp())
    origin = BENCH / meta["id"] / "origin"
    gl = T.FakeGitLab(origin)
    gl.base = gl.start = meta["base"]
    gl.head = meta["head"]
    orig = gl.mr

    def mr(pp, iid):
        m = orig(pp, iid)
        m.attributes["title"] = "update " + Path(meta["files"][0]).name  # 中性标题，避免泄露缺陷类型
        return m

    gl.mr = mr
    cfg = Config(projects=[T.PP], human_reviewer="alden", data_dir=str(tmp / "data"),
                 review=ReviewConfig(static_analysis=StaticAnalysisConfig(enabled=False)))
    cfg.data_path.mkdir(parents=True)
    deps = Deps(settings=Settings(env=Env(), config=cfg), store=Store(cfg.data_path / "state.db"), gl=gl)
    return SimpleNamespace(deps=deps, graph=build_graph(deps), tmp=tmp)


def loc_hit(f: dict, meta: dict) -> bool:
    rs = meta["ranges"].get(f["file"])
    if not rs:
        return False
    lo, hi = f.get("line") or 0, f.get("end_line") or f.get("line") or 0
    return any(lo <= b + MARGIN and hi >= a - MARGIN for a, b in rs)


def judge(f: dict, meta: dict) -> Judge | None:
    msg = (f"下面是一个已知缺陷的描述（来自其修复说明），以及一条代码审查发现。判断这条发现指出的是否就是同一个缺陷"
           f"（同一机制或同一处代码问题即可，措辞不必一致；只是泛泛的相关问题不算）。\n\n## 已知缺陷（修复说明）\n"
           f"标题：{meta['title']}\n{meta['body'][:1200]}\n\n## 审查发现\n{f['file']} L{f.get('line')}\n{f['title']}\n{f['detail'][:800]}")
    try:
        return invoke_structured(Judge, [human(msg)], role="verify")
    except Exception as e:  # noqa: BLE001
        print("   judge 失败:", str(e)[:120], flush=True)
        return None


def run_case(meta: dict, forward: bool = False) -> dict:
    m = meta | ({"base": meta["head"], "head": meta["base"]} if forward else {})
    e = make_env(m)
    t = time.time()
    err = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            e.graph.invoke({"event": {"kind": "new_push", "project_path": T.PP, "mr_iid": 7},
                            "dry_run": False, "full": True}, {"max_concurrency": 1})
    except Exception as ex:  # noqa: BLE001
        err = str(ex)[:300]
    secs = round(time.time() - t)
    fs = [f for f in e.deps.store.findings(T.PP, 7) if f["status"] in ("OPEN", "NEW", "DISPUTED", "VERIFYING", "ESCALATED")]
    out = []
    for f in fs:
        j = judge(f, meta) if f["file"] in meta["ranges"] and not forward else None  # 负例没有“已知缺陷”可比对
        out.append({"sev": f["severity"], "cat": f["category"], "file": f["file"], "line": f.get("line"), "title": f["title"],
                    "loc_hit": loc_hit(f, meta), "judge_hit": bool(j and j.same_defect), "judge_reason": j.reason if j else None})
    shutil.rmtree(e.tmp, ignore_errors=True)
    return {"id": meta["id"] + ("@fwd" if forward else ""), "mode": "fwd" if forward else "rev",
            "source": meta["source"], "secs": secs, "error": err, "n": len(out),
            "loc_hit": any(x["loc_hit"] for x in out), "judge_hit": any(x["judge_hit"] for x in out), "findings": out}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--forward"]
    forward = "--forward" in sys.argv[1:]
    label, only = args[0], set(args[1:])
    resdir = BENCH / "results"; resdir.mkdir(exist_ok=True)
    path = resdir / f"{label}.jsonl"
    done = {json.loads(l)["id"] for l in path.read_text().splitlines()} if path.exists() else set()
    for d in sorted(BENCH.iterdir()):
        if not (d / "meta.json").exists() or (only and d.name not in only) or d.name + ("@fwd" if forward else "") in done:
            continue
        meta = json.loads((d / "meta.json").read_text())
        r = run_case(meta, forward)
        with path.open("a") as fh:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[{label}] {r['id']}: secs={r['secs']} n={r['n']} loc_hit={r['loc_hit']} judge_hit={r['judge_hit']} err={r['error']}", flush=True)
        for x in r["findings"]:
            mark = "★" if x["judge_hit"] else ("◆" if x["loc_hit"] else " ")
            print(f"   {mark} {x['sev']} {x['file'].split('/')[-1]}:{x['line']} {x['title'][:70]}", flush=True)
