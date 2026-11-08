"""单独审图编排：只扫描现有结果图，固定配置并逐张审核，不连接生图网站。

审核线程只调用完整 E；主线程应用删留结果和原子保存操作进度。暂停或故障
停止启动新审核，排空在途工作；未完成操作始终沿用原模型与推理配置。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .atomic_json import atomic_write_json
from .codex_runtime import observe_calls
from .output_store import _no_links, output_identity, read_manifest
from .review_images import ReviewImage
from .review_profiles import ReviewProfile, use_review_profile, validate_review_profile
from .rule_runtime import current_policy
from .saved_images import scan_saved_images, apply_saved_review, counts
from .stage_a_inputs import assert_source_unchanged
from .stage_c_web_plan import load_plan
from .stage_e_codex_review import review_candidate
from .stage_f_outputs import _destination


def _item_key(item):
    candidate = item["candidate"]
    value = [item["row"], item["filename"], candidate["result_id"], candidate["sha256"]]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _open_operation(paths, product, selected_profile):
    """复用未完成操作；完成后创建新记录，允许下一次重审选择另一模型。"""
    if isinstance(selected_profile, dict):
        selected_profile = ReviewProfile.from_dict(selected_profile)
    if not isinstance(selected_profile, ReviewProfile):
        raise TypeError("单独审图需要有效的模型配置")
    output = _destination(product)
    _, canonical = output_identity(output)
    identity = {"product_id": product.product_id, "source_sha256": product.sha256,
                "output_directory": canonical}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    directory = _no_links(paths.root / "saved_reviews" / key)
    directory.mkdir(parents=True, exist_ok=True)
    pointer = _no_links(directory / "active.json")
    if pointer.exists():
        active = json.loads(pointer.read_text(encoding="utf-8"))
        run_id = active.get("run_id")
        if (active.get("schema_version") != 1 or not isinstance(run_id, str) or len(run_id) != 32
                or any(c not in "0123456789abcdef" for c in run_id)):
            raise ValueError("单独审图操作指针无效，未覆盖进度")
        path = _no_links(directory / "runs" / f"{run_id}.json")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("schema_version") != 1 or record.get("run_id") != run_id or any(record.get(k) != v for k, v in identity.items()):
            raise ValueError("单独审图进度不属于当前商品或目录")
        if record.get("status") not in ("running", "paused", "needs_attention", "complete"):
            raise ValueError("单独审图进度状态异常")
        chosen = ReviewProfile.from_dict(record["review_profile"])
        if record["status"] != "complete":
            if record.get("policy_version") != current_policy():
                raise ValueError("未完成单独审图必须继续使用原规则，不能混用新规则")
            return path, record, chosen, True
    run_id = uuid.uuid4().hex
    path = _no_links(directory / "runs" / f"{run_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, "run_id": run_id, **identity,
              "policy_version": current_policy(), "review_profile": selected_profile.to_dict(),
              "status": "running", "created_at": time.time(), "items": {}, "results": {}}
    atomic_write_json(path, record)
    atomic_write_json(pointer, {"schema_version": 1, "run_id": run_id})
    return path, record, selected_profile, False


def run_saved_review(pipeline, product, selected_profile):
    """逐张审查现有图片，返回完成/暂停/需处理；生成提交数恒为零。"""
    assert_source_unchanged(product)
    if pipeline.review_workers not in (1, 2, 4):
        raise ValueError("单独审图并发数只支持 1、2 或 4")
    path, record, chosen, resumed = _open_operation(pipeline.paths, product, selected_profile)
    with use_review_profile(chosen):
        return _run(pipeline, product, path, record, chosen, resumed)


def _run(pipeline, product, path, record, chosen, resumed):
    plan_path = pipeline.paths.tasks / f"product_{product.product_id}_references.json"
    plan = load_plan(plan_path) if plan_path.exists() else None
    if plan is not None and (plan.product_id != product.product_id or plan.source_sha256 != product.sha256):
        raise ValueError("单独审图参考计划不属于当前商品原图")
    items = scan_saved_images(product, pipeline.paths, plan)
    for item in items:
        record["items"][_item_key(item)] = {"row": item["row"], "filename": item["filename"],
                                          "result_id": item["candidate"]["result_id"],
                                          "sha256": item["candidate"]["sha256"]}
    record.update(status="running", updated_at=time.time(), workers=pipeline.review_workers)
    atomic_write_json(path, record)
    pipeline.emit("run_options", "单独审图沿用未完成操作的配置" if resumed else "开始单独审图",
                  product_id=product.product_id, mode="saved_review", review_enabled=True,
                  review_profile=chosen.to_dict(), model=chosen.model, resumed=resumed)

    first_fatal = None
    model_failed = False
    processed = set()
    pending = {}
    position = 0

    def progress():
        completed = sum(x.get("status") == "reviewed" for x in record["results"].values())
        pipeline.emit("saved_review_progress", f"单独审图：已完成 {completed}/{len(record['items'])}",
                      product_id=product.product_id, completed=completed, total=len(record["items"]),
                      errors=sum(x.get("status") == "error" for x in record["results"].values()))

    def worker(item):
        try:
            candidate = ReviewImage(**item["candidate"])
            def phase(event):
                labels = {"started": "开始", "finished": "完成", "retry": "重试当前步骤",
                          "failed": "失败，保留图片", "timeout": "超时，保留图片"}
                elapsed = f"，{event['seconds']:.0f}秒" if "seconds" in event else ""
                pipeline.emit("review_stage", f"{item['filename']}：{event['stage']}{labels.get(event['state'], event['state'])}{elapsed}",
                              product_id=product.product_id, row=item["row"], result_id=candidate.result_id, **event)
            with observe_calls(phase):
                result = review_candidate(product, candidate, pipeline.paths.reviews, model=chosen.model)
            raw = asdict(result) if is_dataclass(result) else dict(result)
            validate_review_profile(raw, model=chosen.model)
            return {"status": "reviewed", "review": raw}
        except (TimeoutError, ConnectionError) as error:
            return {"status": "error", "error": str(error)}
        except OSError as error:
            # 审核缓存读写失败不是图片结论，排空已经开始的其他图片后向外报错。
            return {"status": "fatal", "exception": error}
        except Exception as error:
            return {"status": "error", "error": str(error)}

    def commit(item, outcome):
        nonlocal first_fatal, model_failed
        key = _item_key(item)
        if outcome["status"] == "fatal":
            if first_fatal is None:
                first_fatal = outcome["exception"]
            return
        if outcome["status"] == "error":
            model_failed = True
        try:
            if outcome["status"] == "reviewed":
                validate_review_profile(outcome["review"], model=chosen.model)
                event = apply_saved_review(product, pipeline.paths, item, review=outcome["review"])
            else:
                event = apply_saved_review(product, pipeline.paths, item, error=outcome["error"])
            record["results"][key] = {**outcome, "decision": event["decision"], "finished_at": time.time()}
            record["updated_at"] = time.time()
            atomic_write_json(path, record)
            processed.add(key)
            pipeline.emit("saved_review_image", "单独审图完成一张：" + event["summary"], **event)
            progress()
        except Exception as error:
            # 文件/持久化/回调失败不能降格成模型拒绝；仍收回已经开始的其他审核。
            if first_fatal is None:
                first_fatal = error

    with ThreadPoolExecutor(max_workers=pipeline.review_workers) as pool:
        while pending or position < len(items):
            while (position < len(items) and len(pending) < pipeline.review_workers
                   and not pipeline.stop.is_set() and first_fatal is None and not model_failed):
                item = items[position]; position += 1
                previous = record["results"].get(_item_key(item)) if resumed else None
                if previous and previous.get("status") == "reviewed":
                    # 同操作已完成证据可重用，当前磁盘图片和人工结论仍由应用函数重新核对。
                    commit(item, previous)
                else:
                    future = pool.submit(copy_context().run, worker, item)
                    pending[future] = item
            if not pending:
                break
            ready, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                item = pending.pop(future)
                commit(item, future.result())

    remaining = [item for item in items if _item_key(item) not in processed]
    if first_fatal is not None or model_failed:
        status = "needs_attention"
    elif remaining:
        status = "paused"
    else:
        status = "complete"
    record.update(status=status, updated_at=time.time(), pending_count=len(remaining))
    if first_fatal is not None:
        record["last_error"] = str(first_fatal)
    try:
        atomic_write_json(path, record)
    except Exception:
        if first_fatal is None:
            raise
    if first_fatal is not None:
        raise first_fatal
    assert_source_unchanged(product)
    saved = read_manifest(_destination(product), pipeline.paths.tasks, product)
    result = {"product_id": product.product_id, "status": status, "review_profile": chosen.to_dict(),
              "model": chosen.model, "resumed": resumed, "total": len(record["items"]),
              "reviewed": sum(x.get("status") == "reviewed" for x in record["results"].values()),
              "errors": sum(x.get("status") == "error" for x in record["results"].values()),
              "counts": counts(saved), "operation_path": str(path), "new_generation_submissions": 0}
    pipeline.emit("saved_review_progress", "单独审图已完成" if status == "complete" else "单独审图已保留进度", **result)
    return result
