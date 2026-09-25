from __future__ import annotations

import argparse
import logging
import sys

from .deps import Deps


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ai-cr", description="本地模型 GitLab MR 自动代码审查")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="常驻运行：轮询 + 处理任务")
    sub.add_parser("check", help="检查 GitLab 与本地模型连通性")
    sub.add_parser("poll", help="只轮询一次并入队，不处理")
    r = sub.add_parser("review", help="立即审查指定 MR")
    r.add_argument("project", help="project path，例如 my-group/service-a")
    r.add_argument("iid", type=int)
    r.add_argument("--dry-run", action="store_true", help="只打印结果，不提交到 GitLab、不写状态")
    r.add_argument("--full", action="store_true", help="忽略增量，全量审查")
    s = sub.add_parser("status", help="查看某个 MR 的问题状态")
    s.add_argument("project")
    s.add_argument("iid", type=int)
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    deps = Deps.create()

    if args.cmd == "check":
        from .llm import chat
        print(f"GitLab 账号: @{deps.bot_username}；human_reviewer: @{deps.cfg.human_reviewer} "
              f"(id={deps.gl.user_id(deps.cfg.human_reviewer)})")
        if deps.bot_username == deps.cfg.human_reviewer:
            print("⚠️  bot 与 human_reviewer 是同一个账号：升级 @ 通知不会提醒到你，建议使用单独的 bot 账号。")
        for pp in deps.cfg.projects:
            try:
                deps.gl.project(pp)
                print(f"  ✓ {pp}")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {pp}: {e}")
        reply = chat("review").invoke("只回复 OK").content
        print(f"本地模型 {deps.settings.env.llm_model}: {reply[:50]!r}")
    elif args.cmd == "poll":
        from .poller import poll_once
        print(f"入队 {poll_once(deps)} 个任务")
    elif args.cmd == "review":
        from .worker import make_graph
        graph = make_graph(deps)
        state = graph.invoke(
            {"event": {"kind": "new_push", "project_path": args.project, "mr_iid": args.iid},
             "dry_run": args.dry_run, "full": args.full},
            {"configurable": {"thread_id": f"manual-{args.project}-{args.iid}-{id(graph)}"},
             "max_concurrency": 1, "recursion_limit": 200},
        )
        if not args.dry_run:
            print(state.get("summary", "（跳过：该版本已审查过，使用 --full 强制重审）"))
    elif args.cmd == "status":
        for f in deps.store.findings(args.project, args.iid):
            print(f"[{f['severity']}] {f['status']:<10} {f['file']}:{f['line']}  {f['title']}")
            for e in deps.store.events(f["id"]):
                print(f"      {e['created_at']} {e['actor']}: {e['kind']} {(e['content'] or '')[:80]}")
    elif args.cmd == "run":
        from .worker import serve
        serve(deps)


if __name__ == "__main__":
    sys.exit(main())
