"""逐图就绪调度：网站查询与全局审核池并行，单线程提交审核报告和合格输出。"""

from __future__ import annotations

import copy
import json
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from pathlib import Path

from .review_group_state import write_group_review_summary
from .review_images import ReviewImage, assert_review_image_unchanged
from .review_record_validation import validate_bound_review
from .rule_runtime import current_policy
from .review_profiles import validate_review_profile
from .stage_a_inputs import assert_source_unchanged
from .stage_d_web_generation import group_decision, prepare_job, query_job, submit_job
from .generation_settings import job_resolution, validate_resolution


def terminal(job):
    """只有服务器整组结束且所有槽位明确成功/失败，才允许提交完整审核报告。"""
    return (job.get("state") in ("awaiting_audit", "generation_failed")
            and len(job.get("results", [])) == 5
            and all(r["status"] in ("done", "error") for r in job["results"]))


def _resume_outcomes(product, job, root):
    """恢复同图同规则的已完成检查；拒绝错组、错槽、过期规则、被改动的缓存。"""
    full, partial = root / "round_review.json", root / "partial_review.json"
    path = full if full.exists() else partial
    if not path.exists():
        return {}, False
    report = json.loads(path.read_text(encoding="utf-8"))
    expected = {"product_id": product.product_id, "source_sha256": product.sha256,
                **{k: job[k] for k in ("row", "round_index", "task_id", "folder_id", "reference_ids")}}
    if any(report.get(k) != v for k, v in expected.items()):
        raise ValueError("审核断点不属于当前商品、任务或参考组")
    complete = report.get("all_successful_images_reviewed") is True and report.get("status") == "reviewed"
    if not complete and not (report.get("all_successful_images_reviewed") is False and report.get("status") == "reviewing"):
        raise ValueError("审核断点状态无效")
    successes = {r["result_id"]: r for r in job.get("results", []) if r["status"] == "done"}
    outcomes = {}
    for item in report["outcomes"]:
        candidate = ReviewImage(**item["candidate"])
        result = successes.get(candidate.result_id)
        if (not result or candidate.result_id in outcomes
                or item["reference_id"] != result["reference_id"]
                or item["reference_index"] != result["reference_index"]
                or not Path(candidate.local_path).resolve().is_relative_to(root.resolve())):
            raise ValueError("审核断点图片与已完成槽位不符")
        if item["review"]["policy_version"] != current_policy():
            raise ValueError("审核断点规则已变化，不能静默复用旧结论")
        assert_review_image_unchanged(candidate)
        validate_bound_review(product, candidate, item["review"])
        validate_review_profile(item["review"])
        outcomes[candidate.result_id] = item
    if complete and (not terminal(job) or set(outcomes) != set(successes)):
        raise ValueError("完整审核断点没有覆盖终态任务的全部成功图片")
    return outcomes, complete


def run_streaming_product(pipeline, product, plan, upload, selected, client, *, poll_interval=8, generation_timeout=2100):
    """输入固定商品/九组计划，输出各组最终状态；不改变审核强度、规则或补生判断。"""
    from .pipeline import UserPaused

    if poll_interval <= 0 or generation_timeout <= 0:
        raise ValueError("查询间隔与生成等待上限必须大于零")
    resolution = validate_resolution((pipeline.active_options or {}).get("resolution", "2K"))
    ready = deque()
    states = {g.row: {"group": g, "job": None, "path": None, "outcomes": {}, "queued": set(),
                      "next_poll": 0.0, "deadline": None, "action": None, "finished": False,
                      "full": False, "progress": None} for g in selected}
    results, reviews = {}, {}
    io_future, io_state = None, None
    failure = None
    paused = False

    def remember_error(error):
        nonlocal failure, paused
        if isinstance(error, UserPaused):
            paused = True
        elif failure is None:
            failure = error

    def notify(kind, message, **data):
        """通知失败也停止新任务，但不能跳过在途审核的落盘和排空。"""
        try:
            pipeline.emit(kind, message, **data)
        except Exception as error:
            remember_error(error)

    def adopt(state, path, record):
        """IO完成后交给主协调器，禁止多个线程共享可变任务快照。"""
        job = copy.deepcopy(record)
        group = state["group"]
        if (job.get("product_id") != product.product_id or job.get("source_sha256") != product.sha256
                or job.get("row") != group.row or job.get("folder_id") != group.folder_id
                or job.get("reference_ids") != [r.image_id for r in group.references]
                or job_resolution(job) != resolution):
            raise ValueError("出图任务不属于当前商品或固定参考组")
        new_round = state["job"] is None or state["job"]["round_index"] != job["round_index"]
        state.update(path=Path(path), job=job, action=None)
        if new_round:
            root = pipeline.paths.cache / product.product_id / f"group_{group.row:02}" / f"round_{job['round_index']:02}"
            state["root"] = root
            state["outcomes"], state["full"] = _resume_outcomes(product, job, root)
            state["queued"] = set()
            state["deadline"] = time.monotonic() + generation_timeout
            state["progress"] = None
        state["next_poll"] = time.monotonic() + poll_interval
        progress = (job["state"], job.get("successful_results", 0), job.get("failed_results", 0))
        if progress != state["progress"]:
            notify("generation_progress", "逐张查询出图；完成图片立即进入审核队列",
                          product_id=product.product_id, row=group.row, round_index=job["round_index"],
                          state=job["state"], successful=progress[1], failed=progress[2])
            state["progress"] = progress
        # 先完成先入队；一张慢图不占用其他组的空闲审核位置。
        for result in sorted(job.get("results", []), key=lambda r: r["reference_index"]):
            key = result["result_id"]
            if result["status"] == "done" and key not in state["outcomes"] and key not in state["queued"]:
                state["queued"].add(key)
                ready.append((state, copy.deepcopy(result)))
                notify("image_ready", "图片已生成，进入跨组审核队列", product_id=product.product_id,
                              row=group.row, round_index=job["round_index"], result_id=key,
                              reference_index=result["reference_index"])

    def advance(state):
        """只在当前轮全部审完后沿用原D判断；每组最多一个提交/查询在途。"""
        group = state["group"]
        decision = group_decision(pipeline.paths.tasks, product, group)
        if decision.action in ("complete", "paused"):
            state["finished"] = True
            results[group.row] = {"row": group.row, "state": decision.action, "approved_count": decision.approved_count}
            notify("group_" + decision.action, decision.reason, product_id=product.product_id,
                          row=group.row, approved_count=decision.approved_count)
        elif decision.action == "generate":
            state.update(job=None, path=None, action="generate", next_poll=0.0)
        else:
            directory = pipeline.paths.tasks / product.product_id / f"group_{group.row:02}"
            summary = directory / "review_summary.json"
            reviewed = json.loads(summary.read_text(encoding="utf-8")).get("reviewed_rounds", []) if summary.exists() else []
            pending = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in sorted(directory.glob("round_*.json"))]
            pending = [(p, j) for p, j in pending if j["round_index"] not in reviewed or not terminal(j)]
            if not pending:
                raise ValueError("当前组等待审核但没有对应的未完成轮次")
            adopt(state, *pending[0])

    def website_work(state):
        """网站线程只处理一次查询/提交；暂停后不能开启新的收费请求。"""
        pipeline.check_pause()
        if state["action"] == "generate":
            path, job = prepare_job(product, upload, plan, state["group"].row, pipeline.paths.tasks, client,
                                    resolution=resolution)
        else:
            path = state["path"]
            job = json.loads(path.read_text(encoding="utf-8"))
        pipeline.check_pause()
        if job_resolution(job) != resolution:
            raise ValueError("已有任务出图尺寸与固定配置不一致，未提交或查询")
        if job["state"] == "prepared":
            pipeline.emit("submitting", "提交初轮或按原参考组限次补生", product_id=product.product_id,
                          row=job["row"], round_index=job["round_index"], expected_results=5,
                          quoted_credits=job.get("quoted_credits"))
            return path, submit_job(path, client)
        return path, query_job(path, client)

    def record_outcome(state, outcome):
        """协调器逐张落盘并保存，不从审核线程直接写共享报告或输出清单。"""
        key = outcome["candidate"]["result_id"]
        state["outcomes"][key] = outcome
        job = state["job"]
        pipeline._write_round_report(product, job, list(state["outcomes"].values()), state["root"], complete=False)
        notify("image_reviewed", "一张图片审核完成并保存合格结果", product_id=product.product_id,
                      row=job["row"], round_index=job["round_index"], completed=len(state["outcomes"]),
                      total=5, decision=outcome["review"]["decision"], summary=outcome["review"]["summary"])
        pipeline._save_all(product, plan)

    for state in states.values():
        pipeline.check_pause()
        advance(state)
    with ThreadPoolExecutor(max_workers=pipeline.review_workers) as review_pool, ThreadPoolExecutor(max_workers=1) as website_pool:
        while True:
            paused = paused or pipeline.stop.is_set()
            # 先收回结果。暂停或另一张失败也必须保存已经完整审完的同伴。
            for future in [f for f in reviews if f.done()]:
                state, result = reviews.pop(future)
                try:
                    record_outcome(state, future.result())
                except Exception as error:
                    remember_error(error)
                    notify("review_image_error", "本图审核中断，已完成结果保留", product_id=product.product_id,
                                  row=state["group"].row, result_id=result["result_id"], error=str(error))
            if io_future is not None and io_future.done():
                try:
                    adopt(io_state, *io_future.result())
                except Exception as error:
                    remember_error(error)
                io_future, io_state = None, None
            try:
                for state in states.values():
                    if state["finished"] or state["job"] is None or state is io_state:
                        continue
                    job = state["job"]
                    successful = {r["result_id"] for r in job.get("results", []) if r["status"] == "done"}
                    if terminal(job) and set(state["outcomes"]) == successful:
                        if not state["full"]:
                            pipeline._write_round_report(product, job, list(state["outcomes"].values()), state["root"], complete=True)
                            state["full"] = True
                        write_group_review_summary(product, pipeline.paths.tasks, pipeline.paths.cache, job["row"])
                        pipeline._save_all(product, plan)
                        # 即使暂停也登记完整轮次，但停止创建下一轮。
                        if not failure and not paused:
                            advance(state)
                        else:
                            state["finished"] = True
                if not failure and not paused:
                    assert_source_unchanged(product)
                    while ready and len(reviews) < pipeline.review_workers:
                        pipeline.check_pause()
                        state, result = ready.popleft()
                        job = copy.deepcopy(state["job"])
                        notify("reviewing", "出图即审，跨组共享审核位置", product_id=product.product_id,
                                      row=job["row"], round_index=job["round_index"], total=5,
                                      result_id=result["result_id"])
                        if failure or paused:
                            break
                        # 同组图片并发下载前先建共享目录，避免 Windows 创建父目录的竞态。
                        (pipeline.paths.cache / product.product_id / f"group_{job['row']:02}" /
                         f"round_{job['round_index']:02}").mkdir(parents=True, exist_ok=True)
                        future = review_pool.submit(copy_context().run, pipeline._review_one, product, job, result)
                        reviews[future] = (state, result)
                    if io_future is None and not failure and not paused:
                        now = time.monotonic()
                        due = []
                        for state in states.values():
                            if state["finished"]:
                                continue
                            job = state["job"]
                            if job is not None and not terminal(job) and now >= state["deadline"]:
                                raise TimeoutError("生成跟踪超过35分钟；原任务、已审图片及补生次数保留")
                            if (state["action"] == "generate" or job is not None and not terminal(job)) and now >= state["next_poll"]:
                                due.append(state)
                        if due:
                            io_state = min(due, key=lambda s: (s["next_poll"], s["group"].row))
                            # 复制上下文，确保IO中的补生判断与审核使用同一固定规则。
                            io_future = website_pool.submit(copy_context().run, website_work, copy.copy(io_state))
            except Exception as error:
                remember_error(error)
            for state in states.values():
                if state["finished"] or state["job"] is None:
                    continue
                active = sum(owner is state for owner, _ in reviews.values())
                queued = sum(owner is state for owner, _ in ready)
                progress = (not terminal(state["job"]), active, queued, len(state["outcomes"]))
                if progress != state.get("display_progress"):
                    state["display_progress"] = progress
                    notify("stream_progress", "生图与审图并行处理中", product_id=product.product_id,
                                  row=state["group"].row, round_index=state["job"]["round_index"],
                                  generating=progress[0], active_reviews=active, queued_reviews=queued,
                                  reviewed=progress[3])
            if (failure or paused) and not reviews and io_future is None:
                break
            if all(s["finished"] for s in states.values()) and not reviews and io_future is None:
                break
            pending = list(reviews) + ([io_future] if io_future is not None else [])
            if pending:
                wait(pending, timeout=min(0.2, poll_interval), return_when=FIRST_COMPLETED)
            else:
                pipeline.stop.wait(min(0.2, poll_interval))
    if failure:
        raise failure
    pipeline.check_pause()
    return [results[g.row] for g in selected]
