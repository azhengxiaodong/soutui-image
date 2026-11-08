"""板块 F：原目录下建立商品 ID 和九个组，只复制双次通过且身份匹配的原尺寸图片。

保存原始字节，不重编码；按组对图片内容 SHA-256 去重，不覆盖已有素材。
输出清单与文件锁保存在软件数据目录，保存失败不能伪装成图片已经落盘。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .atomic_json import atomic_write_json
from .review_images import ReviewImage, assert_review_image_unchanged
from .stage_a_inputs import ProductInput, assert_source_unchanged
from .stage_c_web_plan import ProductReferencePlan
from .stage_e_codex_review import (validate_review)
from .rule_runtime import current_policy
from .review_record_validation import validate_bound_review
from .human_review import apply_human_decision, human_decision
from .output_store import (manifest_path, read_manifest, migrate_manifest, output_lock,
                           output_identity, MANIFEST_NAME, LOCK_NAME)


def _direct_path(path: Path, parent: Path):
    if path.resolve().parent != parent.resolve() or path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("输出目录存在链接或越出预期父目录，已停止")


def _destination(product: ProductInput):
    if not product.product_id.isascii() or not product.product_id.isdigit():
        raise ValueError("商品 ID 不能用于安全的输出目录命名")
    base = Path(product.source_path).resolve(strict=True).parent
    target = base / product.product_id
    _direct_path(target, base)
    if target.exists() and not target.is_dir():
        raise FileExistsError("商品 ID 对应路径已是文件，不能覆盖")
    return target


def _output_lock(root: Path, state_directory):
    """兼容 F 的调用名，锁文件存入软件内部目录。"""
    return output_lock(root, state_directory)


def _group_mapping(plan):
    return [{"row": a.row, "directory": f"组{a.row:02}", "folder_id": a.folder_id,
             "group_name": a.group_name, "reference_ids": [r.image_id for r in a.references]}
            for a in plan.assignments]


def _load_or_initialize(root: Path, product: ProductInput, plan: ProductReferencePlan, state_directory):
    path = manifest_path(root, state_directory)
    expected = _group_mapping(plan)
    record = migrate_manifest(root, state_directory, product, plan)
    if record is not None:
        if (record.get("app") != "soutui-image" or record.get("schema_version") != 1
                or record.get("product_id") != product.product_id or record.get("source_sha256") != product.sha256
                or [{k: g[k] for k in expected[0]} for g in record["groups"]] != expected):
            raise ValueError("已有输出清单不属于当前原图或九组计划，不能覆盖")
        return record
    # 不接管有未知素材的同名文件夹，避免把用户旧图混成审核通过的结果。
    for child in root.iterdir():
        if child.name == LOCK_NAME:
            continue
        if child.name not in {g["directory"] for g in expected} or not child.is_dir() or any(child.iterdir()):
            raise FileExistsError("同名商品目录已有未登记内容，未覆盖或混入合格结果")
    record = {"app": "soutui-image", "schema_version": 1, "product_id": product.product_id,
              "source_sha256": product.sha256, "original_file_name": Path(product.source_path).name,
              "output_directory": output_identity(root)[1],
              "groups": [{**group, "saved": []} for group in expected]}
    _save_manifest(path, record)
    return record


def _save_manifest(path, record):
    atomic_write_json(path, record)


def _qualified_items(product, plan, reports, cache_directory, state_directory, *, allow_partial=False,
                     include_pending=False):
    cache = Path(cache_directory).resolve()
    prepared = []
    for report in reports:
        partial = (allow_partial and report.get("status") == "reviewing"
                   and report.get("all_successful_images_reviewed") is False)
        complete = report.get("all_successful_images_reviewed") is True and report.get("status") == "reviewed"
        if (not (partial or complete)
                or report["product_id"] != product.product_id or report["source_sha256"] != product.sha256):
            raise ValueError("当前轮次还没有完成全部逐图审核，或不属于当前原图")
        groups = [g for g in plan.assignments if g.row == report["row"]]
        if len(groups) != 1:
            raise ValueError("审核报告组号与当前九组计划不符")
        group = groups[0]
        reference_ids = [r.image_id for r in group.references]
        if report["folder_id"] != group.folder_id or report["reference_ids"] != reference_ids:
            raise ValueError("审核报告使用了其他参考组")
        job_path = (Path(state_directory) / product.product_id / f"group_{group.row:02}"
                    / f"round_{report['round_index']:02}.json")
        job = json.loads(job_path.read_text(encoding="utf-8"))
        if (job["task_id"] != report["task_id"] or job["source_sha256"] != product.sha256
                or job["folder_id"] != group.folder_id or job["reference_ids"] != reference_ids):
            raise ValueError("审核结果没有对应的当前生成任务")
        # 已出图的子集可以逐张保存，但网站未结束的轮次不能冒充完整报告。
        if complete and (job.get("state") not in ("awaiting_audit", "generation_failed")
                         or any(r.get("status") not in ("done", "error") for r in job["results"])):
            raise ValueError("生成轮次尚未结束，不能消费完整审核报告；请保留逐图审核进度")
        successful = {r["result_id"] for r in job["results"] if r["status"] == "done"}
        reviewed = {o["candidate"]["result_id"] for o in report["outcomes"]}
        if (len(report["outcomes"]) != len(reviewed) or not reviewed.issubset(successful)
                or (complete and reviewed != successful)):
            raise ValueError("审核报告缺少成功结果或混入其他任务图片")
        for outcome in report["outcomes"]:
            candidate = ReviewImage(**outcome["candidate"])
            review = outcome["review"]
            validate_bound_review(product, candidate, review)
            review = apply_human_decision(product, candidate, review, state_directory)
            decision = review["decision"]
            if decision != "pass" and not (include_pending and decision == "review"):
                continue
            if not Path(candidate.local_path).resolve().is_relative_to(cache):
                raise ValueError("待保存图片不在本程序审核缓存范围")
            assert_review_image_unchanged(candidate)
            index = outcome["reference_index"]
            if (type(index) is not int or index not in range(5) or outcome["reference_id"] != reference_ids[index]):
                raise ValueError("当前图片没有对应本组的原参考图")
            prepared.append((group.row, report["round_index"], index, outcome["reference_id"], candidate, review))
    return prepared


def save_approved_reports(product: ProductInput, plan: ProductReferencePlan, reports: list[dict],
                          cache_directory, state_directory, *, allow_partial=False, include_pending=False):
    """完整预检后保存通过图；新任务可显式保留待确认图，旧任务默认行为不变。"""
    assert_source_unchanged(product)
    if (plan.product_id != product.product_id or plan.source_sha256 != product.sha256
            or len(plan.assignments) != 9 or [g.row for g in plan.assignments] != list(range(1, 10))
            or len({g.folder_id for g in plan.assignments}) != 9):
        raise ValueError("当前商品没有完整的九组固定计划")
    if not reports:
        raise ValueError("没有已完成的逐图审核报告，暂不创建最终图片目录")
    qualified = _qualified_items(product, plan, reports, cache_directory, state_directory,
                                 allow_partial=allow_partial, include_pending=include_pending)
    root = _destination(product)
    # 已有无清单的非空目录在加锁前再次检查，不能先留下程序文件再要求用户处理。
    if root.exists() and read_manifest(root, state_directory, product) is None:
        unknown = [p for p in root.iterdir() if p.name != LOCK_NAME and
                   (p.name not in {f"组{i:02}" for i in range(1, 10)} or not p.is_dir() or any(p.iterdir()))]
        if unknown:
            raise FileExistsError("同名商品目录已有未登记内容，未改写")
    root.mkdir(exist_ok=True)
    saved_new, duplicates = 0, 0
    with _output_lock(root, state_directory):
        manifest = _load_or_initialize(root, product, plan, state_directory)
        target_manifest = manifest_path(root, state_directory)
        for group in manifest["groups"]:
            directory = root / group["directory"]
            _direct_path(directory, root)
            directory.mkdir(exist_ok=True)
            for old in group["saved"]:
                human = human_decision(product, old, state_directory)
                if human and human["decision"] == "reject":
                    raise ValueError("用户已否决的图片仍在合格目录，请先完成撤回，不能继续累计合格")
                if old.get("policy_version") != current_policy():
                    raise ValueError("旧规则保存图片必须先撤到待复核区，再按新规则审核，不能直接累计合格")
                if Path(old["filename"]).name != old["filename"]:
                    raise ValueError("旧输出清单含不安全文件名")
                path = directory / old["filename"]
                _direct_path(path, directory)
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != old["sha256"]:
                    raise ValueError("已有合格图片被移除或更改，先核对输出，不能继续累计")
        for row, round_index, index, reference_id, candidate, review in qualified:
            group = manifest["groups"][row - 1]
            # 独立审图已明确删除的图不能被历史通过报告重新复制回来。
            if any(item.get("row") == row and item.get("sha256") == candidate.sha256
                   for item in manifest.get("removed", [])):
                continue
            if any(item["sha256"] == candidate.sha256 for item in group["saved"]):
                duplicates += 1
                continue
            extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[candidate.image_format]
            filename = f"轮{round_index:02}_图{index + 1:02}_{candidate.sha256[:12]}{extension}"
            directory = root / group["directory"]
            target = directory / filename
            _direct_path(target, directory)
            raw = Path(candidate.local_path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != candidate.sha256:
                raise ValueError("复制前图片发生变化，停止保存")
            if target.exists():
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != candidate.sha256:
                    raise FileExistsError("目标位置已有不同内容，未覆盖")
            else:
                temporary = directory / (".soutui-" + candidate.sha256 + ".part")
                _direct_path(temporary, directory)
                with temporary.open("wb") as handle:
                    handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != candidate.sha256:
                    raise ValueError("临时输出图片校验失败")
                os.rename(temporary, target)
            group["saved"].append({"filename": filename, "sha256": candidate.sha256, "result_id": candidate.result_id,
                                   "reference_id": reference_id, "round_index": round_index,
                                   "policy_version": current_policy(), "summary": review["summary"],
                                   "review_status": review["decision"],
                                   "approval_source": "human" if review.get("human_review") else "model"})
            _save_manifest(target_manifest, manifest)
            saved_new += 1
        # F：待复核区的历史只登记新审核结果，不删除留存的原始字节。
        current_reviews = {(o["candidate"]["sha256"], r["row"]): o["review"] for r in reports for o in r["outcomes"]
                           if o["review"]["policy_version"] == current_policy()}
        for item in manifest.get("withdrawn", []):
            review = current_reviews.get((item["sha256"], item["row"]))
            if review:
                item.update(review_status=review["decision"], reviewed_policy=current_policy(), review_summary=review["summary"])
        _save_manifest(target_manifest, manifest)
        assert_source_unchanged(product)
        counts = {g["directory"]: len(g["saved"]) for g in manifest["groups"]}
        approved_counts = {g["directory"]: sum(item.get("review_status", "pass") == "pass"
                                             for item in g["saved"]) for g in manifest["groups"]}
    return {"product_id": product.product_id, "output_directory": str(root), "saved_new": saved_new,
            "duplicates_skipped": duplicates, "approved_counts": approved_counts, "saved_counts": counts,
            "total_saved": sum(counts.values())}
