"""用户人工裁定：按原图、结果图和任务身份覆盖保存决定，不改写模型原始结论。"""

import hashlib
import json
from pathlib import Path

from .rule_runtime import current_policy


def record_path(product, state_directory):
    """人工记录位于当前任务目录，禁止商品标识越出该目录。"""
    root = Path(state_directory).resolve()
    target = root / f"product_{product.product_id}_human_review.json"
    if target.resolve().parent != root or target.is_symlink():
        raise ValueError("人工裁定路径不安全")
    return target


def human_decision(product, candidate, state_directory):
    """只读取用户明确裁定的这一张；身份冲突直接停止，不套用到其他商品或图片。"""
    path = record_path(product, state_directory)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 1 or data.get("authority") != "user"
            or data.get("product_id") != product.product_id
            or data.get("source_sha256") != product.sha256
            or not isinstance(data.get("decisions"), dict)):
        raise ValueError("人工裁定记录与当前原图不符")
    get = candidate.get if isinstance(candidate, dict) else lambda k: getattr(candidate, k)
    item = data["decisions"].get(get("result_id"))
    if item is None:
        return None
    if (item.get("result_sha256") != get("sha256") or item.get("decision") not in ("pass", "reject")
            or not isinstance(item.get("reason"), str) or not item["reason"].strip()
            or item.get("authority") != "user"):
        raise ValueError("人工裁定缺少身份绑定或有效结论")
    if item.get("policy_version") != current_policy():
        raise ValueError("人工裁定绑定的规则已变化，需明确核对后继续")
    return {**item, "record_path": str(path), "record_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def apply_human_decision(product, candidate, review, state_directory):
    """调用方先校验完整模型证据；此处仅叠加用户结论，原 passes 和响应保持原样。"""
    decision = human_decision(product, candidate, state_directory)
    if decision is None:
        return review
    return {**review, "decision": decision["decision"],
            "summary": "用户人工确认：" + decision["reason"], "human_review": decision}
