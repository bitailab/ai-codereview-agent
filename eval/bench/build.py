"""把一个“已知缺陷的修复”做成可审查的用例：
  base = 修复后的代码（正确版本），head = 把修复反向应用后的代码（即“引入这个缺陷的改动”）。
审查员看到的是 base→head 的 diff，标准答案是修复改动涉及的位置（head 一侧的行号范围）。
只反向应用非测试的 .go 文件；仓库只保留改动文件所在目录的直接文件和 go.mod（试点简化，会损失跨包上下文）。
用法：python build.py <cases.json> [case_id ...]  →  data/bench/<id>/{origin,meta.json}"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "data" / "bench"


def run(*a, cwd=None, check=True, inp=None):
    r = subprocess.run(a, cwd=cwd, capture_output=True, text=True, input=inp)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(a[:4])}: {r.stderr[:300]}")
    return r.stdout


def fix_diff(c: dict) -> str:
    kind, num = ("pulls", c["pr"]) if c.get("pr") else ("commits", c["ref"])
    return run("gh", "api", f"repos/{c['repo']}/{kind}/{num}", "-H", "Accept: application/vnd.github.v3.diff")


def split_diff(diff: str) -> dict[str, str]:
    """按文件拆分 unified diff。"""
    out, cur, name = {}, [], None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if name:
                out[name] = "".join(cur)
            name, cur = line.split(" b/", 1)[1].strip(), [line]
        else:
            cur.append(line)
    if name:
        out[name] = "".join(cur)
    return out


def old_ranges(filediff: str) -> list[list[int]]:
    rs = []
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", filediff, re.M):
        s, n = int(m.group(1)), int(m.group(2) or 1)
        rs.append([s, max(s, s + n - 1)])
    return rs


def build(c: dict) -> dict:
    d = OUT / c["id"]
    if (d / "meta.json").exists():
        return json.loads((d / "meta.json").read_text())
    d.mkdir(parents=True, exist_ok=True)
    per_file = split_diff(fix_diff(c))
    files = [f for f in c["files"] if f in per_file]
    if not files:
        raise RuntimeError("修复 diff 里找不到目标文件")
    work = Path(tempfile.mkdtemp())
    run("git", "init", "-q", cwd=work)
    run("git", "remote", "add", "origin", f"https://github.com/{c['repo']}.git", cwd=work)
    run("git", "fetch", "-q", "--depth=1", "--filter=blob:none", "origin", c["ref"], cwd=work)
    dirs = sorted({os.path.dirname(f) for f in files})
    paths = ["go.mod"]
    for dd in dirs:
        ls = run("git", "ls-tree", "FETCH_HEAD", *([dd + "/"] if dd else []), cwd=work)
        paths += [l.split("\t", 1)[1] for l in ls.splitlines() if " blob " in l]
    base_dir = d / "origin"
    if base_dir.exists():
        subprocess.run(["rm", "-rf", str(base_dir)])
    base_dir.mkdir()
    tar = subprocess.run(["git", "archive", "FETCH_HEAD", "--", *dict.fromkeys(paths)], cwd=work, capture_output=True)
    if tar.returncode and b"go.mod" in tar.stderr:  # 有的仓库没有根 go.mod
        paths.remove("go.mod")
        tar = subprocess.run(["git", "archive", "FETCH_HEAD", "--", *dict.fromkeys(paths)], cwd=work, capture_output=True)
    if tar.returncode:
        raise RuntimeError("git archive 失败: " + tar.stderr.decode()[:200])
    subprocess.run(["tar", "-x", "-C", str(base_dir)], input=tar.stdout, check=True)
    g = lambda *a: run("git", *a, cwd=base_dir)  # noqa: E731
    g("init", "-q", "-b", "main"); g("config", "user.email", "bench@x"); g("config", "user.name", "bench")
    g("add", "."); g("commit", "-qm", "base (fixed)")
    base = g("rev-parse", "HEAD").strip()
    g("checkout", "-qb", "feat")
    patch = "".join(per_file[f] for f in files)
    r = subprocess.run(["git", "apply", "-R", "--whitespace=nowarn"], cwd=base_dir, input=patch, text=True, capture_output=True)
    if r.returncode:
        raise RuntimeError("反向应用修复失败: " + r.stderr[:300])
    g("commit", "-qam", "update")
    head = g("rev-parse", "HEAD").strip()
    g("update-ref", "refs/merge-requests/7/head", head)
    meta = {"id": c["id"], "base": base, "head": head, "files": files, "ranges": {f: old_ranges(per_file[f]) for f in files},
            "title": c["title"], "body": c["body"], "source": c["source"], "repo": c["repo"], "url": c.get("url"),
            "cwes": c.get("cwes"), "subtype": c.get("subtype"), "merged_at": c.get("merged_at"),
            "diff_lines": sum(1 for l in patch.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


if __name__ == "__main__":
    cases = json.loads(Path(sys.argv[1]).read_text())
    only = set(sys.argv[2:])
    for c in cases:
        if only and c["id"] not in only:
            continue
        try:
            m = build(c)
            print(f"OK   {c['id']}: {m['files']} ranges={m['ranges']} difflines={m['diff_lines']}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {c['id']}: {e}", flush=True)
