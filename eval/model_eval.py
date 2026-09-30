"""模型对比评测：合成用例 + 生命周期用例 + 真实 MR（可选）。
用法：LLM_MODEL=... LLM_REVIEW_NO_THINK=... python model_eval.py <label> <out.json> [--skip-real]
真实 MR 由环境变量指定：EVAL_REAL_MR=group/project!123（不设置则跳过）
"""
import contextlib, io, json, logging, os, re, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
logging.basicConfig(level=logging.WARNING)
logging.getLogger("ai_cr.graph.nodes").setLevel(logging.INFO)  # 看得到“多轮共识跳过复核 / 复核判定误报”，便于分析误报来源
import test_lifecycle as T  # noqa: E402
from ai_cr.deps import Deps  # noqa: E402
from ai_cr.graph.build import build_graph  # noqa: E402
from ai_cr.settings import Config, Env, ReviewConfig, Settings, StaticAnalysisConfig  # noqa: E402
from ai_cr.store import Store  # noqa: E402

LABEL, OUT = sys.argv[1], sys.argv[2]
REAL_MR = os.environ.get("EVAL_REAL_MR", "")
SKIP_REAL = "--skip-real" in sys.argv or "!" not in REAL_MR

CLEAN_BASE = 'package svc\n\nimport "sync"\n\nvar (\n\tmu    sync.RWMutex\n\tcache = map[string]int{}\n)\n\nfunc Get(k string) int {\n\tmu.RLock()\n\tdefer mu.RUnlock()\n\treturn cache[k]\n}\n'
CLEAN = CLEAN_BASE + '\n// Put 写入缓存，并发安全。\nfunc Put(k string, v int) {\n\tmu.Lock()\n\tdefer mu.Unlock()\n\tcache[k] = v\n}\n'
PARTIAL_FIX = T.BASE.replace("var cache", "var mu sync.Mutex\nvar cache").replace("package svc\n", 'package svc\n\nimport "sync"\n') + '\nfunc Put(k string, v int) {\n\tgo func() {\n\t\tmu.Lock()\n\t\tcache[k] = v\n\t\tmu.Unlock()\n\t}()\n}\n'
FULL_FIX = CLEAN

S3_BASE = 'package svc\n\nimport (\n\t"io"\n\t"net/http"\n)\n\nvar _ = io.EOF\nvar _ = http.MethodGet\n'
S3 = S3_BASE + '\n// Fetch 拉取远端配置。\nfunc Fetch(url string) ([]byte, error) {\n\tresp, err := http.Get(url)\n\tif err != nil {\n\t\treturn nil, err\n\t}\n\treturn io.ReadAll(resp.Body)\n}\n'
S4_BASE = 'package svc\n\ntype User struct {\n\tName string\n}\n\nvar users = map[string]*User{}\n'
S4 = S4_BASE + '\n// NameOf 返回用户名。\nfunc NameOf(id string) string {\n\treturn users[id].Name\n}\n'
S5_BASE = 'package svc\n\nimport "database/sql"\n\nvar _ *sql.DB\n'
S5 = S5_BASE + '\n// Save 记录审计日志。\nfunc Save(db *sql.DB, v string) {\n\t_, _ = db.Exec("INSERT INTO audit(v) VALUES (?)", v)\n}\n'

EXPECT = {
    "S1": r"并发|竞态|race|concurrent|加锁|锁",
    "S3": r"Body|Close|关闭|泄漏|泄露",
    "S4": r"nil|空指针|不存在|panic",
    "S5": r"错误|err|忽略|吞",
}


def make_env(base, head):
    tmp = Path(tempfile.mkdtemp())
    origin = tmp / "origin"; origin.mkdir()
    g = lambda *a: T.git(origin, *a)  # noqa: E731
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (origin / "svc.go").write_text(base); g("add", "."); g("commit", "-qm", "base")
    main = g("rev-parse", "HEAD")
    g("checkout", "-qb", "feat"); (origin / "svc.go").write_text(head); g("commit", "-qam", "change")
    h = g("rev-parse", "HEAD"); g("update-ref", "refs/merge-requests/7/head", h)
    gl = T.FakeGitLab(origin); gl.base = gl.start = main; gl.head = h
    cfg = Config(projects=[T.PP], human_reviewer="alden", data_dir=str(tmp / "data"),
                 review=ReviewConfig(static_analysis=StaticAnalysisConfig(enabled=False)))
    cfg.data_path.mkdir(parents=True)
    deps = Deps(settings=Settings(env=Env(), config=cfg), store=Store(cfg.data_path / "state.db"), gl=gl)
    return SimpleNamespace(deps=deps, gl=gl, origin=origin, graph=build_graph(deps), g=g)


def invoke(e, kind, **payload):
    with contextlib.redirect_stdout(io.StringIO()):
        return e.graph.invoke({"event": {"kind": kind, "project_path": T.PP, "mr_iid": 7, **payload},
                               "dry_run": False, "full": True}, {"max_concurrency": 1})


def push(e, content):
    e.g("checkout", "-q", "feat"); (e.origin / "svc.go").write_text(content); e.g("commit", "-qam", "update")
    e.gl.head = e.g("rev-parse", "HEAD"); e.g("update-ref", "refs/merge-requests/7/head", e.gl.head)


results = {"label": LABEL, "cases": {}}


def record(name, **kw):
    results["cases"][name] = kw
    print(f"[{LABEL}] {name}: " + ", ".join(f"{k}={v}" for k, v in kw.items() if k != "findings"), flush=True)
    for f in kw.get("findings", []):
        print(f"      - {f}", flush=True)


# ---------- 合成审查用例 ----------
for name, base, head, reps in [("S1", T.BASE, T.BUGGY, 2), ("S2", CLEAN_BASE, CLEAN, 2),
                               ("S3", S3_BASE, S3, 1), ("S4", S4_BASE, S4, 1), ("S5", S5_BASE, S5, 1)]:
    for i in range(reps):
        e = make_env(base, head)
        t = time.time()
        invoke(e, "new_push")
        fs = e.deps.store.findings(T.PP, 7)
        titles = [f"{f['severity']} L{f['line']} {f['title']}" for f in fs]
        pat = EXPECT.get(name)
        hit = bool(pat) and any(re.search(pat, f["title"] + f["detail"], re.I) for f in fs)
        fp = len(fs) if not pat else sum(1 for f in fs if not re.search(pat, f["title"] + f["detail"], re.I))
        record(f"{name}#{i+1}", secs=round(time.time() - t), hit=hit if pat else None, n=len(fs), fp=fp, findings=titles)

# ---------- 生命周期：争议复核 / 修复验证 ----------
def race_env():
    e = make_env(T.BASE, T.BUGGY)
    invoke(e, "new_push")
    fs = [f for f in e.deps.store.findings(T.PP, 7) if re.search(EXPECT["S1"], f["title"] + f["detail"], re.I)]
    return e, (fs[0] if fs else None)

e, f = race_env()
if f:
    t = time.time()
    invoke(e, "dev_reply", fingerprint=f["fingerprint"], note_body="不是问题", thread="", author="dev")
    st = next(x for x in e.deps.store.findings(T.PP, 7) if x["fingerprint"] == f["fingerprint"])["status"]
    record("D1 真问题被反驳→应坚持", secs=round(time.time() - t), status=st, ok=st == "ESCALATED")
else:
    record("D1 真问题被反驳→应坚持", ok=None, note="审查阶段未报出竞态，无法测试")

e = make_env(CLEAN_BASE, CLEAN)
fake = {"fingerprint": "fake00000001", "file": "svc.go", "line": 20, "end_line": None, "severity": "P0",
        "category": "concurrency", "title": "cache 写入未加锁，存在并发写 panic",
        "detail": "Put 直接写全局 map cache，多个 goroutine 并发调用时会触发 concurrent map writes。",
        "evidence": "cache[k] = v", "suggestion": None, "status": "OPEN", "discussion_id": "d-fake", "inline": True,
        "first_sha": e.gl.head, "last_checked_sha": e.gl.head, "dispute_rounds": 0, "source": "llm"}
e.deps.store.upsert_finding(T.PP, 7, fake)
e.deps.store.record_review(T.PP, 7, e.gl.head, "REQUEST_CHANGES", "")
t = time.time()
invoke(e, "dev_reply", fingerprint="fake00000001", note_body="Put 里第一行就是 mu.Lock()，defer mu.Unlock()，Get 也用了 RLock，是加锁的。",
       thread="", author="dev")
st = e.deps.store.findings(T.PP, 7)[0]["status"]
record("D2 误报被解释→应接受", secs=round(time.time() - t), status=st, ok=st == "WITHDRAWN")

for name, content, want in [("F1 完整修复→应判已修复", FULL_FIX, True), ("F2 只修一半→不应判已修复", PARTIAL_FIX, False)]:
    e, f = race_env()
    if not f:
        record(name, ok=None, note="审查阶段未报出竞态"); continue
    push(e, content)
    t = time.time()
    invoke(e, "new_push")
    st = next(x for x in e.deps.store.findings(T.PP, 7) if x["fingerprint"] == f["fingerprint"])["status"]
    record(name, secs=round(time.time() - t), status=st, ok=(st == "FIXED") == want)

# ---------- 真实 MR ----------
if not SKIP_REAL:
    from ai_cr.settings import get_settings
    deps = Deps.create()
    deps.settings.config.review.static_analysis.enabled = False  # 只评模型
    _orig_mr = deps.gl.mr

    REAL_PP, REAL_IID = REAL_MR.rsplit("!", 1)[0], int(REAL_MR.rsplit("!", 1)[1])

    def _mr(pp, iid):  # 已合并的 MR 伪装为 opened（dry_run 不写 GitLab）
        m = _orig_mr(pp, iid); m.state = "opened"; return m
    deps.gl.mr = _mr
    g = build_graph(deps)
    t = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        s = g.invoke({"event": {"kind": "new_push", "project_path": REAL_PP, "mr_iid": REAL_IID},
                      "dry_run": True, "full": True}, {"max_concurrency": 1})
    fs = [f for f in s.get("findings", []) if f.get("status") == "OPEN"]
    record(f"R1 真实 {REAL_MR}", secs=round(time.time() - t), n=len(fs), conclusion=s.get("conclusion"),
           findings=[f"{f['severity']} {f['file']} L{f['line']} {f['title']}" + (" [旧记录]" if f.get("id") else "")
                     for f in fs])

json.dump(results, open(OUT, "w"), ensure_ascii=False, indent=1)
