"""手动补图：不足三张的原组各补一轮，始终不审图，暂停续跑不重发任务。

手动操作和任务存入独立目录，允许超过原自动补生三轮；原任务、模板、规则和
审核证据保持不变。仅用户再次点击且上次已结束时才创建新的一轮。
"""

import copy
import json
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from .atomic_json import atomic_write_json
from .generation_settings import existing_resolution, job_resolution
from .output_store import _no_links, output_identity
from .regeneration_files import original_plan, original_rounds, inspect_folders, reconcile_folders
from .review_images import download_result
from .saved_images import save_generated_image
from .stage_a_inputs import assert_source_unchanged
from .stage_b_web_upload import WebUploadReceipt, verify_remote_image
from .stage_c_web_plan import parse_favorites, validate_plan
from .stage_d_web_generation import build_payload, quote_balanced, submit_job, query_job
from .stage_f_outputs import _destination
from .streaming_pipeline import terminal
from .web_session import MusefitReadClient
from .web_transport import WebTransport


def _read(path):
    return json.loads(_no_links(path).read_text(encoding="utf-8"))


def _directory(product, paths):
    return _no_links(paths.root / "manual_regeneration" / product.product_id)


def _identity(product, plan, resolution):
    return {"product_id": product.product_id, "source_sha256": product.sha256,
            "output_directory": output_identity(_destination(product))[1], "resolution": resolution,
            "references": [{"row": g.row, "folder_id": g.folder_id, "group_name": g.group_name,
                            "images": [{"id": r.image_id, "url": r.url} for r in g.references]} for g in plan.assignments]}


def _active_run_id(directory):
    run_id = _read(directory / "active.json").get("run_id")
    if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("重新生图操作指针损坏，未重置")
    return run_id


def _open_operation(product, paths, plan, resolution, inventory):
    """复用未结束的同一操作；只有上一操作结束且当前仍有缺图，才建立新操作。"""
    directory = _directory(product, paths)
    pointer = directory / "active.json"
    identity = _identity(product, plan, resolution)
    if pointer.exists():
        run_id = _active_run_id(directory)
        path = directory / "runs" / f"{run_id}.json"
        record = _read(path)
        if record.get("schema_version") != 1 or record.get("run_id") != run_id or any(record.get(k) != v for k, v in identity.items()):
            raise ValueError("重新生图记录与原图、目录、尺寸或模特模板不符")
        if record.get("status") not in ("running", "paused", "needs_attention", "complete"):
            raise ValueError("重新生图状态异常，未覆盖")
        rows = record.get("rows")
        if not isinstance(rows, list) or not rows or len(set(rows)) != len(rows) or any(type(r) is not int or r not in range(1, 10) for r in rows):
            raise ValueError("重新生图分组记录异常")
        if record["status"] != "complete": return path, record, True
    rows = [item["row"] for item in inventory["groups"] if item["eligible"]]
    if not rows: return None, None, False
    run_id = uuid.uuid4().hex
    path = directory / "runs" / f"{run_id}.json"
    record = {"schema_version": 1, "run_id": run_id, **identity, "rows": rows,
              "status": "running", "created_at": time.time(), "saved_results": {}, "review_enabled": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)
    atomic_write_json(pointer, {"run_id": run_id})
    return path, record, False


def _validate_job(job, product, group, resolution, run_id):
    if (job.get("product_id") != product.product_id or job.get("source_sha256") != product.sha256
            or job.get("row") != group.row or job.get("folder_id") != group.folder_id
            or job.get("reference_ids") != [r.image_id for r in group.references]
            or job_resolution(job) != resolution or job.get("manual_operation") != run_id
            or job.get("payload", {}).get("params", {}).get("inputImageUrls2") != [r.url for r in group.references]
            or type(job.get("round_index")) is not int or job["round_index"] < 1):
        raise ValueError("手动生图任务与当前操作或原模板不一致")


def _prepare_job(product, paths, plan, receipt, group, client, operation):
    """先保存唯一客户端号和完整参数；崩溃恢复使用同一路径，不产生第二次提交。"""
    directory = _directory(product, paths) / "jobs" / f"group_{group.row:02}"
    path = _no_links(directory / f"{operation['run_id']}.json")
    if path.exists():
        job = _read(path)
        _validate_job(job, product, group, operation["resolution"], operation["run_id"])
        credits = job.get("quoted_credits")
        if type(credits) is not int or credits <= 0 or credits % 5:
            raise ValueError("原手动生图报价无效，未重建任务")
        expected = build_payload(product, receipt, group, job["client_task_id"], credits // 5,
                                 resolution=operation["resolution"])
        if job["payload"] != expected: raise ValueError("原手动生图参数改变，未提交或重发")
        return path, job
    rounds = original_rounds(product, paths, group)
    if not rounds or any(r.get("state") not in ("awaiting_audit", "generation_failed") for r in rounds):
        raise ValueError("原生成任务未结束，不能另行重新生图")
    prior_indices = []
    for old in sorted(directory.glob("*.json")):
        record = _read(old)
        _validate_job(record, product, group, operation["resolution"], old.stem)
        prior_indices.append(record["round_index"])
    index = max([r["round_index"] for r in rounds] + prior_indices) + 1
    price = quote_balanced(client.request_json("GET", "/api/web/image-tiers"), operation["resolution"])
    client_id = "mdtask-" + uuid.uuid4().hex
    payload = build_payload(product, receipt, group, client_id, price, resolution=operation["resolution"])
    job = {"schema_version": 1, "product_id": product.product_id, "source_sha256": product.sha256,
           "row": group.row, "folder_id": group.folder_id, "group_name": group.group_name,
           "reference_ids": [r.image_id for r in group.references], "round_index": index,
           "state": "prepared", "client_task_id": client_id, "task_id": None, "payload": payload,
           "expected_results": 5, "quoted_credits": price * 5, "created_at": time.time(),
           "manual_operation": operation["run_id"], "review_enabled": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, job)
    return path, job


def run_regeneration(pipeline, product, *, poll_interval=8, generation_timeout=2100):
    """入口只读盘点→核对原模板/原上传→逐张保存；不进入任何模型或旧图审查入口。"""
    pipeline.check_pause()
    plan = original_plan(product, pipeline.paths)
    inventory = inspect_folders(product, pipeline.paths, plan)
    resolution = existing_resolution(product, pipeline.paths.tasks)
    if resolution is None: raise ValueError("没有可复用的原生成尺寸和任务")
    for item in inventory["groups"]:
        pipeline.emit("regeneration_inventory", f"{item['group_name']}：现有{item['count']}张，{item['reason']}",
                      product_id=product.product_id, row=item["row"], actual_count=item["count"])
    # 未完成操作即使目前已有三张，也必须完成原任务的下载，不能另开一轮。
    directory = _directory(product, pipeline.paths)
    active = directory / "active.json"
    pending = active.exists() and _read(directory / "runs" / f"{_active_run_id(directory)}.json").get("status") != "complete"
    if not pending and not any(item["eligible"] for item in inventory["groups"]):
        blocked = any(item["count"] < 3 for item in inventory["groups"])
        return {"product_id": product.product_id, "status": "needs_attention" if blocked else "complete",
                "groups": inventory["groups"], "new_rounds": 0}
    client = WebTransport(pipeline.paths.session)
    favorites = MusefitReadClient(pipeline.paths.session).get_json("/api/web/favorites", {"mediaType": "image"})
    validate_plan(plan, product, parse_favorites(favorites))
    upload_path = _no_links(pipeline.paths.tasks / f"product_{product.product_id}_upload.json")
    if not upload_path.is_file(): raise ValueError("缺少原图上传回执，重新生图不会重新分类或上传")
    receipt = WebUploadReceipt(**_read(upload_path)["receipt"])
    if (receipt.product_id != product.product_id or receipt.source_sha256 != product.sha256
            or Path(receipt.source_path).resolve() != Path(product.source_path).resolve()):
        raise ValueError("原图上传回执不属于当前商品")
    verify_remote_image(receipt, client)
    pipeline.check_pause()
    inventory = reconcile_folders(product, pipeline.paths, plan)
    path, record, resumed = _open_operation(product, pipeline.paths, plan, resolution, inventory)
    if record is None: return {"product_id": product.product_id, "status": "complete", "groups": [], "new_rounds": 0}
    pipeline.emit("run_options", "继续原重新生图操作" if resumed else "不足3张的组各补一轮，全部保存、不审图",
                  product_id=product.product_id, mode="regenerate", review_enabled=False, resolution=resolution)
    try:
        return _run(pipeline, product, plan, receipt, client, path, record, poll_interval, generation_timeout)
    except Exception:
        record.update(status="paused" if pipeline.stop.is_set() else "needs_attention", updated_at=time.time())
        atomic_write_json(path, record)
        raise


def _run(pipeline, product, plan, receipt, client, operation_path, operation, poll_interval, generation_timeout):
    """网站请求串行，跨组下载四路并行；单张done即保存，原未完成请求只查询。"""
    if poll_interval < 0 or generation_timeout <= 0: raise ValueError("查询或超时参数无效")
    operation["status"] = "running"; atomic_write_json(operation_path, operation)
    states = {}
    for row in operation["rows"]:
        pipeline.check_pause()
        group = plan.assignments[row - 1]
        path, job = _prepare_job(product, pipeline.paths, plan, receipt, group, client, operation)
        states[row] = {"group": group, "path": path, "job": job, "queued": set(),
                       "saved": set(operation["saved_results"].get(str(row), [])), "next_poll": 0.0,
                       "deadline": time.monotonic() + generation_timeout, "finished": False}
    ready, downloads = deque(), {}
    io_future = io_state = failure = None

    def adopt(state, job):
        _validate_job(job, product, state["group"], operation["resolution"], operation["run_id"])
        state["job"] = copy.deepcopy(job); state["next_poll"] = time.monotonic() + poll_interval
        for result in job.get("results", []):
            if result["status"] == "done" and result["result_id"] not in state["saved"] | state["queued"]:
                state["queued"].add(result["result_id"]); ready.append((state, copy.deepcopy(result)))
        pipeline.emit("generation_progress", "原模板重新生图，成功一张保存一张，不审图", product_id=product.product_id,
                      row=job["row"], round_index=job["round_index"], successful=job.get("successful_results", 0),
                      failed=job.get("failed_results", 0), state=job["state"])

    def website_step(state):
        pipeline.check_pause(); assert_source_unchanged(product)
        job = _read(state["path"])
        _validate_job(job, product, state["group"], operation["resolution"], operation["run_id"])
        if job["state"] == "prepared":
            pipeline.emit("submitting", f"使用原模板 {state['group'].group_name} 重新生图一轮", product_id=product.product_id,
                          row=job["row"], round_index=job["round_index"])
            return submit_job(state["path"], client)
        return query_job(state["path"], client)

    for state in states.values(): adopt(state, state["job"])
    with ThreadPoolExecutor(max_workers=4) as download_pool, ThreadPoolExecutor(max_workers=1) as website_pool:
        while True:
            for future in [f for f in downloads if f.done()]:
                state, job, result = downloads.pop(future)
                try:
                    saved = save_generated_image(product, plan, job, result, future.result(), pipeline.paths)
                    state["saved"].add(result["result_id"])
                    operation["saved_results"][str(job["row"])] = sorted(state["saved"])
                    atomic_write_json(operation_path, operation)
                    pipeline.emit("generated_saved", "重新生图已保存（未审核）", **saved)
                except Exception as error:
                    if failure is None: failure = error
            if io_future is not None and io_future.done():
                try: adopt(io_state, io_future.result())
                except Exception as error:
                    if failure is None: failure = error
                io_future = io_state = None
            for state in states.values():
                job = state["job"]
                done = {r["result_id"] for r in job.get("results", []) if r["status"] == "done"}
                if not state["finished"] and terminal(job) and done <= state["saved"]:
                    state["finished"] = True
                    pipeline.emit("generation_saved_complete", "重新生图本轮结束，成功图全部保存（未审核）",
                                  product_id=product.product_id, row=job["row"], round_index=job["round_index"],
                                  successful=len(done), failed=job.get("failed_results", 0))
            if not failure and not pipeline.stop.is_set():
                try:
                    assert_source_unchanged(product)
                    while ready and len(downloads) < 4:
                        state, result = ready.popleft()
                        cache = pipeline.paths.cache / "manual_regeneration" / product.product_id / operation["run_id"] / f"group_{state['group'].row:02}"
                        cache.mkdir(parents=True, exist_ok=True)
                        future = download_pool.submit(download_result, product.product_id, result, cache)
                        downloads[future] = state, copy.deepcopy(state["job"]), result
                    if io_future is None:
                        now = time.monotonic()
                        due = [s for s in states.values() if not s["finished"] and not terminal(s["job"]) and now >= s["next_poll"]]
                        if any(now >= s["deadline"] for s in due): raise TimeoutError("重新生图等待超过35分钟，原任务号保留")
                        if due:
                            io_state = min(due, key=lambda s: (s["next_poll"], s["group"].row))
                            io_future = website_pool.submit(website_step, io_state)
                except Exception as error:
                    if failure is None: failure = error
            if (failure or pipeline.stop.is_set() or all(s["finished"] for s in states.values())) and not downloads and io_future is None: break
            pending = list(downloads) + ([io_future] if io_future is not None else [])
            if pending: wait(pending, timeout=max(.01, min(.2, poll_interval)), return_when=FIRST_COMPLETED)
            else: pipeline.stop.wait(max(.01, min(.2, poll_interval)))
    if failure: raise failure
    pipeline.check_pause()
    operation.update(status="complete", finished_at=time.time())
    atomic_write_json(operation_path, operation)
    final = inspect_folders(product, pipeline.paths, plan)
    result = {"product_id": product.product_id, "status": "complete", "review_enabled": False,
              "run_id": operation["run_id"], "groups": final["groups"], "new_rounds": len(states)}
    pipeline.emit("product_finished", "重新生图一轮已结束；仍不足3张时可再次点击重新生图", **result)
    return result
