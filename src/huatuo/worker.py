"""串行消费任务队列（本地模型一次只跑一个 MR），崩溃后可从 checkpoint 恢复。"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import time

from langgraph.checkpoint.sqlite import SqliteSaver

from .deps import Deps
from .graph.build import build_graph
from .graph.nodes import MrNoLongerOpen
from .graph.render import review_failed_body
from .llm import is_unavailable, model_ready
from .poller import poll_once

log = logging.getLogger(__name__)


def make_graph(deps: Deps):
    conn = sqlite3.connect(deps.cfg.data_path / "checkpoints.db", check_same_thread=False)
    return build_graph(deps, SqliteSaver(conn))


def _config(job_id: int) -> dict:
    return {"configurable": {"thread_id": f"job-{job_id}"}, "max_concurrency": 1, "recursion_limit": 200}


def run_job(deps: Deps, graph, job: dict, resume: bool = False) -> None:
    cfg = _config(job["id"])
    event = {"kind": job["kind"], "project_path": job["project_path"], "mr_iid": job["mr_iid"], **job["payload"]}
    log.info("处理任务 #%s %s %s!%s", job["id"], job["kind"], job["project_path"], job["mr_iid"])
    if resume and graph.get_state(cfg).next:
        log.info("从 checkpoint 恢复任务 #%s", job["id"])
        graph.invoke(None, cfg)
    else:
        graph.invoke({"event": event, "dry_run": False, "full": bool(job["payload"].get("full"))}, cfg)


def drain(deps: Deps, graph) -> None:
    while job := deps.store.next_job():
        try:
            run_job(deps, graph, job, resume=True)
            deps.store.finish_job(job["id"])
        except MrNoLongerOpen as e:  # MR 已关闭/合并：任务直接结束，不标失败、不改评论
            log.info("任务 #%s 中止：%s", job["id"], e)
            deps.store.finish_job(job["id"])
        except Exception as e:  # noqa: BLE001
            if is_unavailable(e):  # 模型中途被卸载/服务重启：放回队列，等模型就绪后从 checkpoint 继续
                log.warning("任务 #%s 中断：模型不可用，放回队列（%s）", job["id"], str(e)[:200])
                deps.store.requeue_job(job["id"])
                return
            log.exception("任务 #%s 失败", job["id"])
            deps.store.finish_job(job["id"], error=str(e)[:2000])
            if job["kind"] == "new_push":
                _mark_review_failed(deps, job)


def _mark_review_failed(deps: Deps, job: dict) -> None:
    """审查开始时汇总评论被改成了“正在审查”，失败后要改掉，否则会一直显示审查中。"""
    pp, iid = job["project_path"], job["mr_iid"]
    note_id = deps.store.mr_state(pp, iid).get("summary_note_id")
    if not note_id:
        return
    try:
        body = review_failed_body(job["payload"].get("head_sha") or "", deps.store.last_summary(pp, iid))
        deps.gl.upsert_note(pp, iid, note_id, body)
    except Exception as e:  # noqa: BLE001
        log.warning("更新审查失败提示失败: %s", e)


def keep_awake() -> None:
    """macOS 闲置睡眠会让轮询和模型推理一起停住。-s 只在接电源时阻止睡眠（用电池时照常睡）；-w 跟随本进程退出，不会遗留。"""
    if caffeinate := shutil.which("caffeinate"):
        subprocess.Popen([caffeinate, "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("已阻止系统闲置睡眠（caffeinate）")


def serve(deps: Deps) -> None:
    keep_awake()
    graph = make_graph(deps)
    for job in deps.store.running_jobs():  # 上次异常退出时正在执行的任务：放回队列，模型就绪后从 checkpoint 继续
        log.info("任务 #%s 上次未完成，重新排队", job["id"])
        deps.store.requeue_job(job["id"])
    interval = deps.cfg.poll_interval_seconds
    model_down: str | None = None
    log.info("开始轮询，间隔 %ss，human_reviewer=@%s，bot=@%s", interval, deps.cfg.human_reviewer, deps.bot_username)
    while True:
        started = time.monotonic()
        try:
            n = poll_once(deps)
            if n:
                log.info("本轮新增 %d 个任务", n)
        except Exception:  # noqa: BLE001
            log.exception("轮询失败")
        # 模型未就绪（开机时尚未加载完成等）时只轮询入队，任务保持 pending，就绪后再处理
        ok, why = model_ready()
        if ok:
            if model_down:
                log.info("模型已就绪，开始处理任务")
            model_down = None
            drain(deps, graph)
        elif why != model_down:
            log.warning("模型未就绪，暂不处理任务：%s", why)
            model_down = why
        time.sleep(max(5.0, interval - (time.monotonic() - started)))
