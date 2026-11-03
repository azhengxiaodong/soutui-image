"""板块 D：按固定设置提交每组五张任务、关联逐张状态，并限制同组补生至三轮。

先持久保存客户端任务号再提交。网络结果不明时只查回任务，不自动重发。
生成完成后必须等待 E/F 提供逐张审核与唯一合格图片哈希，不能把生成成功当作审核通过。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .stage_a_inputs import ProductInput, assert_source_unchanged
from .stage_b_web_upload import WebUploadReceipt, validate_upload_urls
from .stage_c_web_plan import GroupAssignment, ProductReferencePlan
from .web_transport import RequestOutcomeUnknown, WebTransport
from .rule_runtime import current_policy


PROMPT = "如提供了模特图，让模特图中的模特穿上服装实拍图中的衣服；并按照参考图中的人一样在相同的场景中摆出一样的姿势"
MAX_REGENERATION_ROUNDS = 3
MIN_APPROVED_PER_GROUP = 3


@dataclass(frozen=True)
class GenerationDecision:
    action: str
    next_round: int | None
    approved_count: int
    reason: str


def decide_next_round(rounds: list[dict], approved_sha256: list[str], reviewed_rounds: list[int]):
    """初次为第零轮，补生最多第一至第三轮；只累计不重复的合格图片内容哈希。"""
    if not all(isinstance(s, str) and re.fullmatch(r"[a-f0-9]{64}", s) for s in approved_sha256):
        raise ValueError("审核统计必须使用实际图片 SHA-256，不能用图片数量或文件名代替")
    approved = len(set(approved_sha256))
    indices = sorted(r["round_index"] for r in rounds)
    if indices != list(range(len(indices))) or any(type(i) is not int or i > 3 for i in indices):
        raise ValueError("补生轮次记录不连续或已超出三轮上限")
    if not rounds:
        return GenerationDecision("generate", 0, approved, "尚未初次生成")
    if any(r["state"] not in ("awaiting_audit", "generation_failed") for r in rounds):
        return GenerationDecision("wait", None, approved, "存在未完成或提交状态不明的任务，先查询任务")
    if set(indices) - set(reviewed_rounds):
        return GenerationDecision("audit", None, approved, "成功结果仍需全部逐张审核，不能提前补生或停止")
    if approved >= MIN_APPROVED_PER_GROUP:
        return GenerationDecision("complete", None, approved, "本组累计合格至少三张")
    if max(indices) >= MAX_REGENERATION_ROUNDS:
        return GenerationDecision("paused", None, approved, "已用完三轮补生，仍不足三张，暂停该组并提示")
    return GenerationDecision("generate", max(indices) + 1, approved, "本组累计合格不足三张，沿用原来的五张参考图")


def quote_balanced_2k(response: dict) -> int:
    """从网站实际模式报价取得单张积分；不自行假定或修改后台费用。"""
    if response.get("success") is not True or not response.get("enabled"):
        raise ValueError("网站图像模式配置不可用")
    if "model-dressing" not in response.get("features", []):
        raise ValueError("当前账号的模特换衣不支持图像模式选择")
    choices = [t for t in response.get("tiers", []) if t.get("id") == "balanced"]
    if len(choices) != 1:
        raise ValueError("无法唯一确定均衡模式")
    tier = choices[0]
    if not tier.get("available") or "2K" not in tier.get("availableResolutions", []):
        raise ValueError("当前账号不能使用均衡模式 2K")
    price = tier.get("prices", {}).get("2K")
    if type(price) is not int or price <= 0:
        raise ValueError("网站没有给出有效的均衡 2K 单价")
    return price


def build_payload(product: ProductInput, upload: WebUploadReceipt, group: GroupAssignment,
                  client_task_id: str, unit_price: int):
    """保持与网页批量模特换衣相同的字段：原图一张、参考五张、每张一份、立即提交。"""
    assert_source_unchanged(product)
    if (upload.product_id != product.product_id or upload.source_sha256 != product.sha256
            or upload.remote_sha256 != product.sha256 or upload.category not in {"套装", "上装", "下装"}):
        raise ValueError("上传回执与当前原图不符")
    validate_upload_urls(upload.image_url, upload.image_url)
    if (len(group.references) != 5 or len({r.image_id for r in group.references}) != 5
            or group.row not in range(1, 10) or type(unit_price) is not int or unit_price <= 0):
        raise ValueError("当前组不是五张不同参考图或参数无效")
    references = [r.url for r in group.references]
    for url in references:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("参考图 URL 无效")
    form = {"clothingName": f"{product.product_id}-组{group.row:02}", "gender": "女装", "ageGroup": "成人",
            "isSensitive": "false", "resultsPerReference": "1", "selectedPoseCount": "5", "hasModel": "no",
            "headSubMode": "face-hair", "garmentType": upload.category}
    params = {"imageUrls": [upload.image_url, *references], "prompt": PROMPT,
              "extraParams": {**form, "imageTier": "balanced"}, "resolution": "2K", "aspectRatio": "3:4",
              "inputImageUrls": [upload.image_url], "inputImageUrls2": references,
              "inputSnapshot": {"kind": "model-dressing", "clothingImageUrls": [upload.image_url],
                                "referenceImageUrls": references, "formParams": form},
              "creditsCharged": unit_price * 5}
    return {"featureType": "model-dressing", "params": params, "clientTaskId": client_task_id}


def _save(path: Path, record: dict):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _group_directory(directory, product_id, row):
    return Path(directory).resolve() / product_id / f"group_{row:02}"


def group_decision(directory, product: ProductInput, group: GroupAssignment):
    """从持久轮次和 E/F 审核汇总判断下一步；缺少审核记录时保持等待。"""
    root = _group_directory(directory, product.product_id, group.row)
    rounds = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob("round_*.json"))]
    for record in rounds:
        if (record["product_id"] != product.product_id or record["source_sha256"] != product.sha256
                or record["folder_id"] != group.folder_id
                or record["reference_ids"] != [r.image_id for r in group.references]):
            raise ValueError("已有生成记录与当前商品或原参考组不一致")
    review_path = root / "review_summary.json"
    approved, reviewed = [], []
    if review_path.exists():
        review = json.loads(review_path.read_text(encoding="utf-8"))
        if (review["product_id"] != product.product_id or review["folder_id"] != group.folder_id
                or review["source_sha256"] != product.sha256
                or review["reference_ids"] != [r.image_id for r in group.references]):
            raise ValueError("审核汇总不属于当前原图和固定参考组")
        # D：旧审核汇总不能继续声称达标，也不能据旧数补生；原轮次全部保留，先完成新版审核。
        if review.get("policy_version") == current_policy():
            approved, reviewed = review["approved_sha256"], review["reviewed_rounds"]
    return decide_next_round(rounds, approved, reviewed)


def prepare_job(product: ProductInput, upload: WebUploadReceipt, plan: ProductReferencePlan,
                row: int, directory, transport: WebTransport) -> tuple[Path, dict]:
    """准备允许的下一轮，保存固定任务号与完整参数；没有通过补生判断就不新建轮次。"""
    assert_source_unchanged(product)
    if plan.product_id != product.product_id or plan.source_sha256 != product.sha256:
        raise ValueError("九组计划与当前商品不对应")
    groups = [g for g in plan.assignments if g.row == row]
    if len(groups) != 1:
        raise ValueError("指定组号不存在或重复")
    group = groups[0]
    root = _group_directory(directory, product.product_id, row)
    previous = sorted(root.glob("round_*.json"))
    if previous:
        latest = json.loads(previous[-1].read_text(encoding="utf-8"))
        decision = group_decision(directory, product, group)
        if decision.action != "generate":
            return previous[-1], latest
        round_index = decision.next_round
    else:
        round_index = 0
    price = quote_balanced_2k(transport.request_json("GET", "/api/web/image-tiers"))
    client_id = "mdtask-" + uuid.uuid4().hex
    payload = build_payload(product, upload, group, client_id, price)
    record = {"schema_version": 1, "product_id": product.product_id, "source_sha256": product.sha256,
              "row": row, "folder_id": group.folder_id, "group_name": group.group_name,
              "reference_ids": [r.image_id for r in group.references], "round_index": round_index,
              "state": "prepared", "client_task_id": client_id, "task_id": None, "payload": payload,
              "expected_results": 5, "quoted_credits": price * 5, "created_at": time.time()}
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"round_{round_index:02}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    return path, record


def _verify_task_parameters(task, record):
    """服务器回显的参数必须属于当前任务；不依靠时间或列表位置关联结果。"""
    params = record["payload"]["params"]
    if task.get("featureType") != "model-dressing" or task.get("expectedResultCount") != 5:
        raise ValueError("服务器任务类型或结果数量不符合当前组")
    for key in ("resolution", "aspectRatio", "inputImageUrls", "inputImageUrls2"):
        if task.get(key) != params[key]:
            raise ValueError(f"服务器任务 {key} 与当前固定设置不符")
    extra = task.get("extraParams", {})
    for key in ("imageTier", "garmentType", "resultsPerReference", "selectedPoseCount"):
        if extra.get(key) != params["extraParams"][key]:
            raise ValueError("服务器回显的模式、类型或每张数量不符")


def _apply_task(path: Path, record: dict, task: dict):
    _verify_task_parameters(task, record)
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("服务器任务缺少 ID")
    known = record.get("task_id")
    if known and known != task_id:
        raise ValueError("任务 ID 变化，拒绝关联其他结果")
    record["task_id"] = task_id
    record["server_status"] = task.get("status")
    slots = task.get("resultSlots") or []
    if not isinstance(slots, list) or len(slots) != 5:
        raise ValueError("服务器没有返回五个逐张结果槽位，不能确认参考图对应关系")
    if {s.get("referenceIndex") for s in slots} != set(range(5)):
        raise ValueError("结果没有逐一对应原组的五张参考图")
    if len({s.get("id") for s in slots}) != 5:
        raise ValueError("结果槽位 ID 重复")
    results = []
    for slot in slots:
        index = slot["referenceIndex"]
        url = slot.get("imageUrl")
        if slot.get("status") == "done" and (not isinstance(url, str) or urlsplit(url).scheme != "https"):
            raise ValueError("成功结果没有可用 HTTPS 图片链接")
        results.append({"result_id": task_id + "/" + str(slot["id"]), "slot_id": slot["id"],
                        "reference_index": index, "reference_id": record["reference_ids"][index],
                        "status": slot.get("status"), "image_url": url if slot.get("status") == "done" else None})
    record["results"] = results
    record["successful_results"] = sum(r["status"] == "done" for r in results)
    record["failed_results"] = sum(r["status"] == "error" for r in results)
    terminal_slots = all(r["status"] in ("done", "error") for r in results)
    if task.get("status") in ("done", "error") and terminal_slots:
        record["state"] = "awaiting_audit" if record["successful_results"] else "generation_failed"
    else:
        record["state"] = "generating"
    record["last_observed_at"] = time.time()
    _save(path, record)
    return record


def submit_job(path: Path, transport: WebTransport):
    """只允许 prepared → 单次 POST；未知结果和已提交状态均转入只读查询。"""
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["state"] != "prepared":
        return query_job(path, transport)
    record["state"] = "submitting"
    record["submit_attempted_at"] = time.time()
    _save(path, record)
    try:
        response = transport.request_json("POST", "/api/web/tasks/submit", body=record["payload"])
    except Exception:
        record["state"] = "submission_unknown"
        _save(path, record)
        raise
    task = response.get("task")
    if not isinstance(task, dict):
        record["state"] = "submission_unknown"
        _save(path, record)
        raise RequestOutcomeUnknown("提交没有返回任务对象；需查询已保存的客户端任务号，不能直接重发")
    # 即使回显校验失败，也先保存已收到的服务器 ID，防止后续误以为没有提交。
    record["task_id"] = task.get("id")
    record["state"] = "submitted_needs_verification"
    _save(path, record)
    return _apply_task(path, record, task)


def query_job(path: Path, transport: WebTransport):
    """该站点无单任务 GET；分页读列表后按精确任务号查询，不按最新一张猜结果。"""
    record = json.loads(path.read_text(encoding="utf-8"))
    expected_id = record.get("task_id") or record["client_task_id"]
    for page in range(20):
        response = transport.request_json("GET", "/api/web/tasks", query={
            "featureType": "model-dressing", "limit": 200, "offset": page * 200})
        if response.get("success") is not True:
            raise RuntimeError("读取任务列表未成功，未重发生成请求")
        matches = [t for t in response.get("tasks", []) if t.get("id") == expected_id]
        if len(matches) > 1:
            raise ValueError("任务列表返回重复任务号")
        if matches:
            return _apply_task(path, record, matches[0])
        if not response.get("hasMore"):
            break
    raise RequestOutcomeUnknown("尚未查回已记录的任务号，保留当前记录并停止；不会重复扣费重发")


def watch_job(path: Path, transport: WebTransport, *, timeout=2100, interval=8, on_progress=None):
    """有上限地只读跟踪；回调只在状态/数量改变时触发，超时保留任务号且不重发。"""
    if timeout <= 0 or interval <= 0:
        raise ValueError("跟踪超时与间隔必须大于零")
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        record = query_job(path, transport)
        state = (record["state"], record.get("successful_results"), record.get("failed_results"))
        if state != previous and on_progress:
            on_progress(record)
        previous = state
        if record["state"] in ("awaiting_audit", "generation_failed"):
            return record
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            record["state"] = "generation_timeout"
            _save(path, record)
            raise TimeoutError("任务跟踪已到时间上限；保留任务号，可稍后查询，不能直接重新提交")
        time.sleep(min(interval, remaining))
