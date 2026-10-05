"""收集试点集候选：GoBench GoReal 的修复 PR、GitHub Advisory 的 Go 漏洞修复提交、热门项目近期的修复 PR。
只做筛选（小改动：1-2 个非测试 .go 文件、新增+删除不超过 80 行），输出 JSON 供人工挑选。
用法：python candidates.py goreal <gobench 的 goreal 目录> | advisory | mined <owner/repo> ..."""
import json, re, subprocess, sys

REPO_OF = {"cockroach": "cockroachdb/cockroach", "etcd": "etcd-io/etcd", "grpc": "grpc/grpc-go", "hugo": "gohugoio/hugo",
           "istio": "istio/istio", "kubernetes": "kubernetes/kubernetes", "moby": "moby/moby", "serving": "knative/serving",
           "syncthing": "syncthing/syncthing"}


def gh(*a):
    r = subprocess.run(["gh", *a], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def small_go(files: list[dict]) -> list[str] | None:
    src = [f for f in files if f["path"].endswith(".go") and not f["path"].endswith("_test.go")]
    if not 1 <= len(src) <= 2 or sum(f["additions"] + f["deletions"] for f in src) > 80:
        return None
    return [f["path"] for f in src]


def pr_info(repo: str, n: int) -> dict | None:
    out = gh("pr", "view", str(n), "--repo", repo, "--json", "number,title,body,files,mergedAt,url,state")
    if not out:
        return None
    d = json.loads(out)
    paths = small_go(d["files"])
    if not paths or d["state"] != "MERGED":
        return None
    return {"repo": repo, "ref": f"refs/pull/{n}/head", "pr": n, "url": d["url"], "title": d["title"], "body": (d["body"] or "")[:1500],
            "files": paths, "merged_at": d["mergedAt"]}


def goreal(root: str):
    import os
    for kind in ("blocking", "nonblocking"):
        for proj in sorted(os.listdir(f"{root}/{kind}")):
            for n in sorted(os.listdir(f"{root}/{kind}/{proj}")):
                if not n.isdigit() or proj not in REPO_OF:
                    continue
                c = pr_info(REPO_OF[proj], int(n))
                if c:
                    readme = open(f"{root}/{kind}/{proj}/{n}/README.md").read()
                    m = re.search(r"\| *(Blocking|Nonblocking)[^|]*\|([^|]*)\|([^|]*)\|", readme)
                    print(json.dumps(c | {"source": "goreal", "kind": kind, "subtype": (m.group(2).strip() + "/" + m.group(3).strip()) if m else ""},
                                     ensure_ascii=False), flush=True)


def advisory(pages: int = 3):
    for page in range(1, pages + 1):
        out = gh("api", f"/advisories?ecosystem=go&type=reviewed&per_page=100&page={page}")
        for a in json.loads(out or "[]"):
            commits = [r for r in a.get("references") or [] if re.search(r"github\.com/[^/]+/[^/]+/commit/[0-9a-f]{40}", r)]
            if len(commits) != 1 or a["severity"] not in ("critical", "high", "medium"):
                continue
            m = re.search(r"github\.com/([^/]+/[^/]+)/commit/([0-9a-f]{40})", commits[0])
            repo, sha = m.group(1), m.group(2)
            c = json.loads(gh("api", f"repos/{repo}/commits/{sha}") or "{}")
            files = [{"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"]} for f in c.get("files", [])]
            paths = small_go(files)
            if paths:
                print(json.dumps({"source": "advisory", "repo": repo, "ref": sha, "ghsa": a["ghsa_id"], "severity": a["severity"],
                                  "cwes": [x["cwe_id"] for x in a.get("cwes", [])], "title": a["summary"], "body": (a["description"] or "")[:1500],
                                  "files": paths, "merged_at": c["commit"]["committer"]["date"], "url": commits[0]}, ensure_ascii=False), flush=True)


def mined(repos: list[str]):
    for repo in repos:
        out = gh("pr", "list", "--repo", repo, "--state", "merged", "--search", "fix in:title (panic OR race OR leak OR deadlock OR nil OR overflow OR lock)",
                 "--limit", "40", "--json", "number")
        for p in json.loads(out or "[]"):
            c = pr_info(repo, p["number"])
            if c:
                print(json.dumps(c | {"source": "mined"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    {"goreal": lambda: goreal(sys.argv[2]), "advisory": advisory, "mined": lambda: mined(sys.argv[2:])}[sys.argv[1]]()
