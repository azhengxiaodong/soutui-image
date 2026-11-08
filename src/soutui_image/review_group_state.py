"""审核状态汇总：只有完成逐张审核的轮次才能驱动同组补生，不把数量估计当作通过。"""

import json
import hashlib
from pathlib import Path

from .atomic_json import atomic_write_json
from .stage_a_inputs import ProductInput, assert_source_unchanged
from .stage_e_codex_review import (validate_review)
from .rule_runtime import current_policy
from .review_record_validation import validate_bound_review
from .human_review import apply_human_decision


def write_group_review_summary(product: ProductInput, state_directory, cache_directory, row: int, *, allow_policy_revision=False):
    """核对真实轮次与每张审核身份，将唯一合格内容哈希交给 D；没有完成的轮次继续等待。"""
    assert_source_unchanged(product)
    from .saved_images import review_overrides
    overrides = review_overrides(product, state_directory, row)
    group_root = Path(state_directory).resolve() / product.product_id / f"group_{row:02}"
    reports = Path(cache_directory).resolve() / product.product_id / f"group_{row:02}"
    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(group_root.glob("round_*.json"))]
    if not jobs:
        raise ValueError("没有当前商品和组号的真实生成记录")
    folder_id, reference_ids = jobs[0]["folder_id"], jobs[0]["reference_ids"]
    approved, reviewed, details, rechecked = set(), [], [], set()
    human_rejected = set()
    for job in jobs:
        if (job["product_id"] != product.product_id or job["source_sha256"] != product.sha256
                or job["folder_id"] != folder_id or job["reference_ids"] != reference_ids or job["row"] != row):
            raise ValueError("生成轮次中混入其他商品或参考组")
        report_path = reports / f"round_{job['round_index']:02}" / "round_review.json"
        if not report_path.exists():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if (report["product_id"] != product.product_id or report["source_sha256"] != product.sha256
                or report["task_id"] != job["task_id"] or report["folder_id"] != folder_id
                or report["reference_ids"] != reference_ids or report["row"] != row
                or report["round_index"] != job["round_index"]):
            raise ValueError("审核报告与当前生成轮次不对应")
        if not report.get("all_successful_images_reviewed"):
            continue
        # 流式逐图审核必须等网站整轮终态，才能累计完整轮次并允许补生判断。
        if (job.get("state") not in ("awaiting_audit", "generation_failed")
                or any(r.get("status") not in ("done", "error") for r in job["results"])):
            raise ValueError("生成轮次尚未结束，不能将逐图审核子集计为完整轮次")
        successful = {r["result_id"] for r in job["results"] if r["status"] == "done"}
        outcomes = report["outcomes"]
        if (len(outcomes) != len(successful)
                or {item["candidate"]["result_id"] for item in outcomes} != successful):
            raise ValueError("审核报告没有逐一覆盖全部成功结果")
        for item in outcomes:
            candidate, review = item["candidate"], item["review"]
            validate_bound_review(product, candidate, review)
            review = apply_human_decision(product, candidate, review, state_directory)
            decision = review["decision"]
            decision = overrides.get(candidate["sha256"], decision)
            if review.get("human_review") and decision == "reject":
                human_rejected.add(candidate["sha256"])
            if review["policy_version"] == current_policy():
                rechecked.add(candidate["sha256"])
            if decision == "pass":
                approved.add(candidate["sha256"])
                details.append({"result_id": candidate["result_id"], "sha256": candidate["sha256"],
                                "round_index": job["round_index"], "report_path": str(report_path)})
        reviewed.append(job["round_index"])
    target = group_root / "review_summary.json"
    if target.exists():
        previous = json.loads(target.read_text(encoding="utf-8"))
        if (previous["product_id"] != product.product_id or previous["source_sha256"] != product.sha256
                or previous["folder_id"] != folder_id or previous["reference_ids"] != reference_ids):
            raise ValueError("已有审核汇总不属于当前商品或参考组，未覆盖")
        if not set(previous["reviewed_rounds"]).issubset(reviewed):
            raise ValueError("旧的已审轮次证据缺失，不能降低合格计数后继续补生")
        version_change = previous.get("policy_version") != current_policy()
        if version_change and not allow_policy_revision:
            raise ValueError("旧汇总需通过明确的规则升级入口迁移，不能静默更新")
        if version_change and not set(previous["approved_sha256"]).issubset(rechecked):
            raise ValueError("旧合格图未全部按新规则重审，不能迁移计数")
        if not version_change and not set(previous["approved_sha256"]).issubset(approved | human_rejected | set(overrides)):
            raise ValueError("旧的合格图片证据缺失，不能静默降低合格数量")
        if version_change:
            raw = target.read_bytes()
            history = group_root / "review_history"; history.mkdir(exist_ok=True)
            backup = history / ("summary_" + hashlib.sha256(raw).hexdigest() + ".json")
            if not backup.exists(): backup.write_bytes(raw)
    summary = {"schema_version": 1, "policy_version": current_policy(), "product_id": product.product_id, "source_sha256": product.sha256,
               "row": row, "folder_id": folder_id, "reference_ids": reference_ids,
               "reviewed_rounds": sorted(reviewed), "approved_sha256": sorted(approved),
               "approved_count": len(approved), "approved_results": details}
    atomic_write_json(target, summary)
    return summary
