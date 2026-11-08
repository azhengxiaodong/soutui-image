"""只生图模式：后台逐张完成就下载保存，不调用审核、不按合格数量补生。"""

from __future__ import annotations

import copy
import json
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from pathlib import Path

from .review_images import download_result
from .saved_images import save_generated_image
from .stage_a_inputs import assert_source_unchanged
from .stage_d_web_generation import prepare_job, query_job, submit_job
from .generation_settings import job_resolution, validate_resolution
from .streaming_pipeline import terminal


def run_generation_only(pipeline, product, plan, upload, selected, client, *, poll_interval=8, generation_timeout=2100):
    """每组仅第0轮；失败槽位如实登记，成功槽位全部保存后结束，不冒充合格或补生。"""
    from .pipeline import UserPaused
    if poll_interval < 0 or generation_timeout <= 0:
        raise ValueError("查询间隔不能为负，生图等待上限必须大于零")
    resolution = validate_resolution((pipeline.active_options or {}).get("resolution", "2K"))
    states = {g.row: {"group": g, "job": None, "path": None, "queued": set(), "saved": set(),
                      "next_poll": 0.0, "deadline": None, "finished": False} for g in selected}
    ready = deque()
    downloads = {}
    io_future = io_owner = None
    failure = None

    def adopt(state, path, job):
        group = state["group"]
        if (job["product_id"] != product.product_id or job["source_sha256"] != product.sha256
                or job["row"] != group.row or job["folder_id"] != group.folder_id
                or job["reference_ids"] != [r.image_id for r in group.references] or job["round_index"] != 0
                or job_resolution(job) != resolution):
            raise ValueError("只生图任务身份异常或出现非初轮任务，已停止")
        state.update(path=Path(path), job=copy.deepcopy(job), next_poll=time.monotonic() + poll_interval)
        if state["deadline"] is None:
            state["deadline"] = time.monotonic() + generation_timeout
        snapshot = (job["state"], job.get("successful_results", 0), job.get("failed_results", 0))
        if snapshot != state.get("progress"):
            state["progress"] = snapshot
            pipeline.emit("generation_progress", "逐张查询出图，成功图片直接保存，不审核", product_id=product.product_id,
                          row=group.row, round_index=0, state=job["state"], successful=snapshot[1], failed=snapshot[2])
        for result in job.get("results", []):
            if result["status"] == "done" and result["result_id"] not in state["queued"]:
                state["queued"].add(result["result_id"])
                ready.append((state, copy.deepcopy(result)))

    def website_step(state):
        pipeline.check_pause()
        if state["path"] is None:
            path, job = prepare_job(product, upload, plan, state["group"].row, pipeline.paths.tasks, client,
                                    resolution=resolution)
        else:
            path = state["path"]
            job = json.loads(path.read_text(encoding="utf-8"))
        pipeline.check_pause()
        if job_resolution(job) != resolution:
            raise ValueError("已有任务出图尺寸与固定配置不一致，未提交或查询")
        if job["round_index"] != 0:
            raise ValueError("关闭审图的任务不能创建补生轮次")
        if job["state"] == "prepared":
            pipeline.emit("submitting", "提交首轮生图（审图关闭）", product_id=product.product_id,
                          row=job["row"], round_index=0)
            return path, submit_job(path, client)
        return path, query_job(path, client)

    for state in states.values():
        pipeline.check_pause()
        directory = pipeline.paths.tasks / product.product_id / f"group_{state['group'].row:02}"
        jobs = sorted(directory.glob("round_*.json"))
        if len(jobs) > 1:
            raise ValueError("只生图任务中已有补生记录，未改写或清空")
        if jobs:
            adopt(state, jobs[0], json.loads(jobs[0].read_text(encoding="utf-8")))

    with ThreadPoolExecutor(max_workers=4) as download_pool, ThreadPoolExecutor(max_workers=1) as website_pool:
        while True:
            for future in [f for f in downloads if f.done()]:
                state, job, result = downloads.pop(future)
                try:
                    candidate = future.result()
                    saved = save_generated_image(product, plan, job, result, candidate, pipeline.paths)
                    state["saved"].add(result["result_id"])
                    pipeline.emit("generated_saved", "图片已保存（未审核）", **saved)
                except Exception as error:
                    if failure is None: failure = error
            if io_future is not None and io_future.done():
                try:
                    adopt(io_owner, *io_future.result())
                except Exception as error:
                    if failure is None: failure = error
                io_future = io_owner = None
            try:
                for state in states.values():
                    job = state["job"]
                    if not state["finished"] and job and terminal(job):
                        successes = {r["result_id"] for r in job["results"] if r["status"] == "done"}
                        if state["saved"] == successes:
                            state["finished"] = True
                            pipeline.emit("generation_saved_complete", "本组生成结束，成功图片已全部保存（未审核）",
                                          product_id=product.product_id, row=state["group"].row,
                                          successful=len(successes), failed=job.get("failed_results", 0))
                if not failure and not pipeline.stop.is_set():
                    assert_source_unchanged(product)
                    while ready and len(downloads) < 4:
                        pipeline.check_pause()
                        state, result = ready.popleft()
                        job = copy.deepcopy(state["job"])
                        root = pipeline.paths.cache / product.product_id / f"group_{job['row']:02}" / "round_00"
                        # 同组下载共用父目录，由协调线程预建，避免 Windows 并发 mkdir 竞态。
                        root.mkdir(parents=True, exist_ok=True)
                        future = download_pool.submit(download_result, product.product_id, result, root)
                        downloads[future] = (state, job, result)
                    if io_future is None:
                        now = time.monotonic()
                        due = []
                        for state in states.values():
                            if state["finished"] or state["job"] and terminal(state["job"]): continue
                            if state["deadline"] is not None and now >= state["deadline"]:
                                raise TimeoutError("生图跟踪超过35分钟，原任务和已保存图片保留")
                            if now >= state["next_poll"]: due.append(state)
                        if due:
                            io_owner = min(due, key=lambda s: (s["next_poll"], s["group"].row))
                            io_future = website_pool.submit(copy_context().run, website_step, copy.copy(io_owner))
            except UserPaused:
                pass
            except Exception as error:
                if failure is None: failure = error
            if (failure or pipeline.stop.is_set()) and not downloads and io_future is None:
                break
            if all(s["finished"] for s in states.values()) and not downloads and io_future is None:
                break
            pending = list(downloads) + ([io_future] if io_future is not None else [])
            if pending:
                wait(pending, timeout=min(.2, poll_interval), return_when=FIRST_COMPLETED)
            else:
                pipeline.stop.wait(min(.2, poll_interval))
    if failure:
        raise failure
    pipeline.check_pause()
    return [{"row": s["group"].row, "state": "complete" if not s["job"].get("failed_results") else "needs_attention",
             "successful_count": len(s["saved"]), "failed_count": s["job"].get("failed_results", 0),
             "approved_count": 0, "review_enabled": False} for s in states.values()]
