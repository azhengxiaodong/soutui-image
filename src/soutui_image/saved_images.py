"""结果图片目录：保存未审图、登记本地图片，以及按完整审核结论删除明确错误图。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .human_review import apply_human_decision
from .output_store import output_lock, read_manifest, migrate_manifest, save_manifest, _no_links
from .review_images import inspect_image, assert_review_image_unchanged
from .review_record_validation import validate_bound_review
from .rule_runtime import current_policy
from .stage_a_inputs import assert_source_unchanged
from .stage_f_outputs import _destination, _load_or_initialize


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def counts(record):
    """未审、待确认、失败均不计入合格；兼容旧版没有状态字段的通过记录。"""
    return {g["row"]: {"saved": len(g["saved"]),
                       "pass": sum(x.get("review_status", "pass") == "pass" for x in g["saved"]),
                       "review": sum(x.get("review_status") in ("review", "error") for x in g["saved"]),
                       "unreviewed": sum(x.get("review_status") == "unreviewed" for x in g["saved"])}
            for g in (record or {}).get("groups", [])}


def _image_path(root, row, name):
    if type(row) is not int or row not in range(1, 10) or Path(name).name != name:
        raise ValueError("图片分组或文件名不安全")
    path = _no_links(root / f"组{row:02}" / name)
    if path.parent != root / f"组{row:02}" or path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("结果图片路径越界或格式不支持")
    return path


def save_generated_image(product, plan, job, result, candidate, paths):
    """关闭审图时原字节逐张保存；必须绑定真实成功槽位，绝不伪造审核通过。"""
    assert_source_unchanged(product)
    assert_review_image_unchanged(candidate)
    row = job["row"]
    group = plan.assignments[row - 1]
    if (job["product_id"] != product.product_id or job["source_sha256"] != product.sha256
            or candidate.product_id != product.product_id or candidate.result_id != result["result_id"]
            or group.row != row or group.folder_id != job["folder_id"]
            or job["reference_ids"] != [r.image_id for r in group.references]
            or result not in job["results"] or result["status"] != "done"
            or result["reference_id"] != job["reference_ids"][result["reference_index"]]
            or not Path(candidate.local_path).resolve().is_relative_to(paths.cache.resolve())):
        raise ValueError("未审图片与生成任务、原图或参考组不对应")
    root = _destination(product)
    root.mkdir(exist_ok=True)
    with output_lock(root, paths.tasks):
        record = _load_or_initialize(root, product, plan, paths.tasks)
        for g in record["groups"]:
            _no_links(root / g["directory"]).mkdir(exist_ok=True)
        saved = record["groups"][row - 1]["saved"]
        removed = any(x.get("row") == row and x.get("sha256") == candidate.sha256
                      for x in record.get("removed", []))
        existing = next((x for x in saved if x["sha256"] == candidate.sha256), None)
        if existing:
            path = _image_path(root, row, existing["filename"])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != candidate.sha256:
                raise ValueError("已有输出图片被移除或更改，未自动覆盖")
        elif not removed:
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[candidate.image_format]
            name = f"轮{job['round_index']:02}_图{result['reference_index'] + 1:02}_{candidate.sha256[:12]}{suffix}"
            path = _image_path(root, row, name)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != candidate.sha256:
                    raise ValueError("同名输出图片内容冲突，未覆盖")
            else:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".soutui-", suffix=".part", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(Path(candidate.local_path).read_bytes())
                        handle.flush(); os.fsync(handle.fileno())
                    if hashlib.sha256(temporary.read_bytes()).hexdigest() != candidate.sha256:
                        raise ValueError("下载图片保存校验失败")
                    os.rename(temporary, path)
                finally:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()
            saved.append({"filename": name, "sha256": candidate.sha256, "result_id": candidate.result_id,
                          "reference_id": result["reference_id"], "round_index": job["round_index"],
                          "review_status": "unreviewed", "policy_version": None,
                          "approval_source": None, "summary": "生成成功，尚未审核"})
            save_manifest(root, paths.tasks, record)
        return {"product_id": product.product_id, "row": row,
                "saved_new": int(not existing and not removed), "output_directory": str(root),
                "total_saved": sum(x["saved"] for x in counts(record).values()), "counts": counts(record)}


def _recover_deletions(product, root, tasks, record):
    """明确删除事务已先记录完整审核证据；中断续跑仍先核对文件和原图再继续。"""
    changed = False
    for item in record.get("removed", []):
        if item.get("deletion_status") != "pending":
            continue
        raw = item["audit_review"]
        validate_bound_review(product, item, raw)
        if apply_human_decision(product, item, raw, tasks)["decision"] != "reject":
            raise ValueError("删除事务缺少当前有效的不合格结论")
        path = _image_path(root, item["row"], item["filename"])
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("待删除图片已经变化，保留图片并停止")
            assert_source_unchanged(product)
            path.unlink()
        group = record["groups"][item["row"] - 1]
        group["saved"] = [x for x in group["saved"] if x["filename"] != item["filename"]]
        item["deletion_status"] = "complete"
        changed = True
    if changed:
        save_manifest(root, tasks, record)


def scan_saved_images(product, paths, plan=None):
    """扫描当前商品九个结果组；原图在父目录，永远不作为待删除的结果图。"""
    assert_source_unchanged(product)
    root = _destination(product)
    if not root.exists():
        return []
    candidates = []
    with output_lock(root, paths.tasks):
        record = migrate_manifest(root, paths.tasks, product, plan)
        if record is None:
            record = {"schema_version": 1, "app": "soutui-image", "product_id": product.product_id,
                      "source_sha256": product.sha256, "original_file_name": Path(product.source_path).name,
                      "groups": [{"row": row, "directory": f"组{row:02}", "saved": [],
                                  "folder_id": plan.assignments[row - 1].folder_id if plan else None,
                                  "reference_ids": [r.image_id for r in plan.assignments[row - 1].references] if plan else [],
                                  "group_name": plan.assignments[row - 1].group_name if plan else "本地结果"}
                                 for row in range(1, 10)]}
        _recover_deletions(product, root, paths.tasks, record)
        changed = False
        for group in record["groups"]:
            directory = _no_links(root / group["directory"])
            if not directory.exists():
                if group["saved"]:
                    raise ValueError("已登记的结果组目录缺失")
                continue
            known = {x["filename"]: x for x in group["saved"]}
            actual = {p.name: p for p in directory.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES}
            if set(known) - set(actual):
                raise ValueError("已登记图片在目录中缺失，请先核对")
            for name in sorted(actual):
                path = _image_path(root, group["row"], name)
                entry = known.get(name)
                result_id = entry["result_id"] if entry else "local/" + hashlib.sha256(str(path).encode()).hexdigest()
                candidate = inspect_image(product.product_id, result_id, path)
                if entry and entry["sha256"] != candidate.sha256:
                    raise ValueError("已登记图片内容改变，未覆盖记录或删除图片")
                if entry is None:
                    entry = {"filename": name, "sha256": candidate.sha256, "result_id": result_id,
                             "review_status": "unreviewed", "policy_version": None, "summary": "本地图片尚未审核"}
                    group["saved"].append(entry); changed = True
                candidates.append({"row": group["row"], "filename": name, "candidate": asdict(candidate)})
        if changed or read_manifest(root, paths.tasks, product) is None:
            save_manifest(root, paths.tasks, record)
    return candidates


def apply_saved_review(product, paths, item, review=None, error=None):
    """只在审图明确拒绝且图片哈希不变时删除；疑难和调用失败一律留在原目录。"""
    assert_source_unchanged(product)
    root = _destination(product)
    candidate = item["candidate"]
    with output_lock(root, paths.tasks):
        record = migrate_manifest(root, paths.tasks, product)
        if record is None:
            raise ValueError("单独审核缺少对应的输出登记")
        group = record["groups"][item["row"] - 1]
        entry = next((x for x in group["saved"] if x["filename"] == item["filename"]), None)
        path = _image_path(root, item["row"], item["filename"])
        if (entry is None or entry["sha256"] != candidate["sha256"] or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != candidate["sha256"]):
            raise ValueError("审核期间结果图片发生变化，未删除或覆盖")
        if error is not None:
            entry.update(review_status="error", summary="审核未完成，图片保留：" + str(error))
            decision = "error"
        else:
            validate_bound_review(product, candidate, review)
            effective = apply_human_decision(product, candidate, review, paths.tasks)
            decision = effective["decision"]
            entry.update(review_status=decision, summary=effective["summary"], policy_version=current_policy(),
                         audit_review=review, review_profile=review.get("review_profile"),
                         approval_source="human" if effective.get("human_review") else "model")
            if decision == "reject":
                record.setdefault("removed", []).append({**entry, "product_id": product.product_id,
                                                          "row": item["row"], "deletion_status": "pending"})
                save_manifest(root, paths.tasks, record)
                _recover_deletions(product, root, paths.tasks, record)
        save_manifest(root, paths.tasks, record)
        return {"product_id": product.product_id, "row": item["row"], "filename": item["filename"],
                "decision": decision, "summary": entry["summary"], "counts": counts(record)}


def review_overrides(product, tasks, row):
    """已保存图片的明确复审结论用于计数；保留原始生成审核报告不改写。"""
    record = read_manifest(_destination(product), tasks, product)
    if record is None:
        return {}
    entries = [*record["groups"][row - 1]["saved"],
               *(x for x in record.get("removed", []) if x.get("row") == row)]
    overrides = {}
    for entry in entries:
        if entry.get("deletion_status") == "external_missing":
            # 外部删图不是新的模型结论；旧通过证据保留，但不能再计作现有合格图。
            overrides[entry["sha256"]] = "review"
            continue
        # 复审调用失败后图片需待确认，不能再沿用原轮次或上次复审的 pass 凑合格数。
        # 原 audit_review 继续留存为历史证据，不把网络/缓存错误伪造成模型拒绝。
        if entry.get("review_status") == "error":
            overrides[entry["sha256"]] = "review"
            continue
        raw = entry.get("audit_review")
        if raw and raw.get("policy_version") == current_policy():
            validate_bound_review(product, {**entry, "product_id": product.product_id}, raw)
            effective = apply_human_decision(product, entry, raw, tasks)
            overrides[entry["sha256"]] = effective["decision"]
    return overrides
