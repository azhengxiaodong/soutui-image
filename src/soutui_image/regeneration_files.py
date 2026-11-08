"""手动重新生图的只读盘点及外部删图登记；不删除图片、不调用模型或网站。"""

import copy
import json
from pathlib import Path

from .output_store import _no_links, read_manifest, output_lock, save_manifest, migrate_manifest
from .review_images import inspect_image
from .saved_images import scan_saved_images, IMAGE_SUFFIXES
from .stage_a_inputs import assert_source_unchanged
from .stage_c_web_plan import load_plan, validate_plan, FavoriteGroup
from .stage_f_outputs import _destination


def original_plan(product, paths):
    """必须有该商品原来的模板及五张参考图；缺失时停止，绝不重新随机选择。"""
    path = _no_links(paths.tasks / f"product_{product.product_id}_references.json")
    if not path.is_file():
        raise ValueError("缺少原模特模板记录，不能重新生图；请保留原任务数据")
    plan = load_plan(path)
    validate_plan(plan, product, tuple(FavoriteGroup(g.folder_id, g.group_name, g.references) for g in plan.assignments))
    return plan


def original_rounds(product, paths, group):
    """核对原生成任务身份，只读原轮次；未生成或生成未结束的组不另起新任务。"""
    root = _no_links(paths.tasks / product.product_id / f"group_{group.row:02}")
    records = []
    for path in sorted(root.glob("round_*.json")):
        _no_links(path)
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("product_id") != product.product_id or record.get("source_sha256") != product.sha256
                or record.get("row") != group.row or record.get("folder_id") != group.folder_id
                or record.get("reference_ids") != [r.image_id for r in group.references]):
            raise ValueError("原生成任务与当前原图或模特模板不符")
        records.append(record)
    if [r.get("round_index") for r in records] != list(range(len(records))):
        raise ValueError("原生成轮次不连续，未重置或另起任务")
    return records


def inspect_folders(product, paths, plan):
    """按实际可解码图片文件计数；已被手动删掉的图片不再计入，内容被替换则停止。"""
    assert_source_unchanged(product)
    root = _no_links(_destination(product))
    if not root.is_dir():
        raise ValueError("尚无已生成图片文件夹，请先使用开始生图")
    record = read_manifest(root, paths.tasks, product)
    if record is not None:
        actual = [(g["row"], g.get("folder_id"), g.get("reference_ids")) for g in record["groups"]]
        expected = [(g.row, g.folder_id, [r.image_id for r in g.references]) for g in plan.assignments]
        if actual != expected:
            raise ValueError("图片目录与原模特模板记录不一致")
    groups, missing = [], []
    for group in plan.assignments:
        directory = _no_links(root / f"组{group.row:02}")
        known = {item["filename"]: item for item in record["groups"][group.row - 1]["saved"]} if record else {}
        actual = {}
        if directory.exists():
            if not directory.is_dir(): raise ValueError("结果组路径不是目录")
            for path in sorted(directory.iterdir()):
                _no_links(path)
                if path.suffix.lower() not in IMAGE_SUFFIXES: continue
                if not path.is_file(): raise ValueError("结果图片路径不是普通文件")
                candidate = inspect_image(product.product_id, "inventory/" + path.name, path)
                if path.name in known and known[path.name]["sha256"] != candidate.sha256:
                    raise ValueError("已登记图片内容被更改，请先核对，未覆盖或重新生图")
                actual[path.name] = candidate.sha256
        missing.extend({**copy.deepcopy(item), "row": group.row} for name, item in known.items() if name not in actual)
        rounds = original_rounds(product, paths, group)
        reason = ("原任务尚未生成" if not rounds else
                  "原生成任务尚未结束，请使用开始继续原任务" if any(r.get("state") not in ("awaiting_audit", "generation_failed") for r in rounds) else
                  "已有至少3张图片" if len(actual) >= 3 else "不足3张，复用原模板补生一轮")
        groups.append({"row": group.row, "group_name": group.group_name, "count": len(actual),
                       "eligible": bool(rounds) and len(actual) < 3 and all(r.get("state") in ("awaiting_audit", "generation_failed") for r in rounds),
                       "reason": reason, "files": actual})
    return {"groups": groups, "missing": missing}


def reconcile_folders(product, paths, plan):
    """明确点击重新生图后登记外部删图，保留原证据并防止旧通过报告把删除图恢复。"""
    root = _destination(product)
    with output_lock(root, paths.tasks):
        inventory = inspect_folders(product, paths, plan)
        record = migrate_manifest(root, paths.tasks, product, plan)
        if record is not None and inventory["missing"]:
            for item in inventory["missing"]:
                group = record["groups"][item["row"] - 1]
                group["saved"] = [x for x in group["saved"] if x["filename"] != item["filename"]]
                record.setdefault("removed", []).append({**item, "product_id": product.product_id,
                    "deletion_status": "external_missing", "removal_reason": "重新生图盘点时文件已被外部移除"})
            save_manifest(root, paths.tasks, record)
    # 为仍在目录中的未知本地图建立未审登记；使用原计划，不编造通过结论。
    scan_saved_images(product, paths, plan)
    return inventory
