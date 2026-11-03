"""E 的规则升级复核：只重审旧通过图，写独立版本报告，不改变原任务和最终图片。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
import threading
from contextvars import copy_context

from .review_images import ReviewImage, assert_review_image_unchanged
from .stage_a_inputs import assert_source_unchanged
from .stage_e_codex_review import (review_candidate)
from .rule_runtime import current_policy


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def plan_revision(products, task_directory, cache_directory):
    """输入当前原图、任务和缓存；核对旧输出与真实结果，输出需重审的逐张清单。"""
    tasks, cache = Path(task_directory).resolve(), Path(cache_directory).resolve()
    reports, pending, source_hashes = [], [], {}
    for product in products:
        assert_source_unchanged(product)
        source_hashes[product.product_id] = product.sha256
        manifest = Path(product.source_path).parent / product.product_id / ".搜推生图.json"
        if not manifest.exists():
            continue
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        if saved["product_id"] != product.product_id or saved["source_sha256"] != product.sha256:
            raise ValueError("旧保存清单不属于当前原图")
        indexed_saved = {(g["row"], item["sha256"]): (g, item) for g in saved["groups"] for item in g["saved"]}
        covered = set()
        for report_path in sorted((cache / product.product_id).glob("group_*/round_*/round_review.json")):
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (report["product_id"] != product.product_id or report["source_sha256"] != product.sha256
                    or not report.get("all_successful_images_reviewed")):
                raise ValueError("原审核报告身份不符或未完成")
            row, round_index = report["row"], report["round_index"]
            job_path = tasks / product.product_id / f"group_{row:02}" / f"round_{round_index:02}.json"
            job = json.loads(job_path.read_text(encoding="utf-8"))
            if any(report[k] != job[k] for k in ("product_id", "source_sha256", "row", "round_index", "task_id", "folder_id", "reference_ids")):
                raise ValueError("旧报告与原生成任务不符")
            successful = {r["result_id"] for r in job["results"] if r["status"] == "done"}
            if (len(report["outcomes"]) != len(successful) or
                    {o["candidate"]["result_id"] for o in report["outcomes"]} != successful):
                raise ValueError("旧报告没有覆盖每张真实成功结果")
            relative = report_path.relative_to(cache)
            reports.append({"path": str(report_path), "sha256": _hash(report_path), "relative": str(relative),
                            "job_path": str(job_path), "job_sha256": _hash(job_path)})
            for outcome in report["outcomes"]:
                candidate = ReviewImage(**outcome["candidate"])
                review = outcome["review"]
                if (candidate.product_id != product.product_id or review["product_id"] != product.product_id
                        or review["source_sha256"] != product.sha256 or review["result_sha256"] != candidate.sha256
                        or review["result_id"] != candidate.result_id):
                    raise ValueError("旧审核与图片身份不符")
                if review["decision"] != "pass":
                    continue
                if not Path(candidate.local_path).resolve().is_relative_to(cache):
                    raise ValueError("旧通过图不在审核缓存内")
                assert_review_image_unchanged(candidate)
                key = (row, candidate.sha256)
                if key in indexed_saved:
                    group, item = indexed_saved[key]
                    if group["folder_id"] != report["folder_id"] or group["reference_ids"] != report["reference_ids"]:
                        raise ValueError("旧合格输出串入其他参考组")
                    if Path(item["filename"]).name != item["filename"]:
                        raise ValueError("旧保存文件名不安全")
                    photo = manifest.parent / group["directory"] / item["filename"]
                    if photo.is_symlink() or photo.resolve().parent != (manifest.parent / f"组{row:02}").resolve():
                        raise ValueError("旧输出路径越界")
                    if _hash(photo) != candidate.sha256:
                        raise ValueError("旧合格输出内容改变")
                    covered.add(key)
                if review["policy_version"] == current_policy():
                    continue
                pending.append({"product_id": product.product_id, "row": row, "round_index": round_index,
                                "candidate": asdict(candidate), "old_policy_version": review["policy_version"],
                                "report_relative": str(relative)})
        if set(indexed_saved) != covered:
            raise ValueError("存在没有原逐张审核证据的旧保存图片，不能猜测其来源")
        reports.append({"path": str(manifest), "sha256": _hash(manifest), "output_manifest": True})
    return {"policy_version": current_policy(), "source_sha256": source_hashes,
            "inputs": reports, "candidates": pending, "candidate_count": len(pending)}


def execute_revision(products, plan, directory, review_directory, *, reuse_directories=(), workers=2, model=None, on_event=None, stop_event=None):
    """逐张实际调用 E，输出新版本报告；中断可续，绝不改原报告、轮次或合格文件。"""
    if plan["policy_version"] != current_policy() or workers not in (1, 2, 4):
        raise ValueError("复核规则或并发参数不符")
    target = Path(directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    settings = {"policy_version": current_policy(), "model": model or "default"}
    settings_path = target / "run_config.json"
    if settings_path.exists() and json.loads(settings_path.read_text(encoding="utf-8")) != settings:
        raise ValueError("复核目录已使用其他审核模型，不能混用旧结论")
    if not settings_path.exists():
        _write(settings_path, settings)
    products_by_id = {p.product_id: p for p in products}
    for p in products:
        assert_source_unchanged(p)
        if plan["source_sha256"].get(p.product_id) != p.sha256:
            raise ValueError("复核计划不属于当前原图")
    for item in plan["inputs"]:
        if _hash(item["path"]) != item["sha256"]:
            raise ValueError("制定计划后的旧记录发生变化，未改写")
    plan_path = target / "plan.json"
    if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")) != plan:
        raise ValueError("复核目录已属于其他输入或规则版本")
    if not plan_path.exists():
        _write(plan_path, plan)
    reuse = {}
    for directory in reuse_directories:
        for path in Path(directory).glob("*/review.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("policy_version") == current_policy():
                identity = (raw["product_id"], raw["result_id"], raw["source_sha256"], raw["result_sha256"])
                expected_key = hashlib.sha256("\0".join((current_policy(), *identity, model or "default")).encode()).hexdigest()
                if path.parent.name == expected_key:
                    reuse[identity] = path.parent.parent
    completed = []
    halted = threading.Event()
    def worker(item):
        if halted.is_set() or (stop_event is not None and stop_event.is_set()):
            return {**item, "status":"paused", "error":"已停止启动新审核，保留已有进度"}
        product = products_by_id[item["product_id"]]
        candidate = ReviewImage(**item["candidate"])
        identity = (product.product_id, candidate.result_id, product.sha256, candidate.sha256)
        try:
            result = review_candidate(product, candidate, reuse.get(identity, review_directory), model=model)
            return {**item, "status": "reviewed", "review": asdict(result)}
        except Exception as error:
            halted.set()
            return {**item, "status": "error", "error": str(error)}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for future in as_completed([executor.submit(copy_context().run, worker, item) for item in plan["candidates"]]):
            result = future.result()
            completed.append(result)
            _write(target / "progress.json", completed)
            if on_event:
                on_event({"completed": len(completed), "total": len(plan["candidates"]),
                          "product_id": result["product_id"], "row": result["row"], "status": result["status"],
                          "decision": result.get("review", {}).get("decision"), "error": result.get("error")})
    errors = [r for r in completed if r["status"] != "reviewed"]
    if errors:
        result = {**settings, "status": "incomplete", "errors": errors,
                  "reviewed": len(completed) - len(errors), "total": len(completed)}
        _write(target / "result.json", result)
        return result
    replacements = {(r["report_relative"], r["candidate"]["result_id"]): r["review"] for r in completed}
    staged = []
    for item in plan["inputs"]:
        if item.get("output_manifest"):
            continue
        if _hash(item["path"]) != item["sha256"] or _hash(item["job_path"]) != item["job_sha256"]:
            raise ValueError("模型运行期间原记录改变，不能提交新版审核")
        report = json.loads(Path(item["path"]).read_text(encoding="utf-8"))
        legacy_nonpass = 0
        for outcome in report["outcomes"]:
            identity = (item["relative"], outcome["candidate"]["result_id"])
            if identity in replacements:
                outcome["review"] = replacements[identity]
            elif outcome["review"]["decision"] == "pass" and outcome["review"]["policy_version"] != current_policy():
                raise ValueError("旧通过图没有新审核，不能视为本次复核完成")
            else:
                legacy_nonpass += 1
        for decision, key in (("pass", "pass_count"), ("reject", "reject_count"), ("review", "review_count")):
            report[key] = sum(o["review"]["decision"] == decision for o in report["outcomes"])
        report["quality_revision"] = {"policy_version": current_policy(), "old_report_sha256": item["sha256"],
                                      "retained_legacy_nonpass": legacy_nonpass}
        path = target / "reports" / item["relative"]
        _write(path, report)
        staged.append({"path": str(path), "sha256": _hash(path), "old_path": item["path"], "old_sha256": item["sha256"]})
    result = {**settings, "status": "staged", "total": len(completed),
              "pass_count": sum(r["review"]["decision"] == "pass" for r in completed),
              "reject_count": sum(r["review"]["decision"] == "reject" for r in completed),
              "review_count": sum(r["review"]["decision"] == "review" for r in completed), "reports": staged,
              "canonical_reports_changed": False, "output_photos_changed": False, "generation_submitted": False}
    _write(target / "result.json", result)
    return result
