"""审核状态汇总：只有完成逐张审核的轮次才能驱动同组补生，不把数量估计当作通过。"""

import json
import hashlib
from pathlib import Path

from .stage_a_inputs import ProductInput, assert_source_unchanged
from .stage_e_codex_review import (validate_review)
from .rule_runtime import current_policy
from .review_record_validation import validate_bound_review


def write_group_review_summary(product: ProductInput, state_directory, cache_directory, row: int, *, allow_policy_revision=False):
    """核对真实轮次与每张审核身份，将唯一合格内容哈希交给 D；没有完成的轮次继续等待。"""
    assert_source_unchanged(product)
    group_root = Path(state_directory).resolve() / product.product_id / f"group_{row:02}"
    reports = Path(cache_directory).resolve() / product.product_id / f"group_{row:02}"
    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(group_root.glob("round_*.json"))]
    if not jobs:
        raise ValueError("没有当前商品和组号的真实生成记录")
    folder_id, reference_ids = jobs[0]["folder_id"], jobs[0]["reference_ids"]
    approved, reviewed, details, rechecked = set(), [], [], set()
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
        successful = {r["result_id"] for r in job["results"] if r["status"] == "done"}
        outcomes = report["outcomes"]
        if (len(outcomes) != len(successful)
                or {item["candidate"]["result_id"] for item in outcomes} != successful):
            raise ValueError("审核报告没有逐一覆盖全部成功结果")
        for item in outcomes:
            candidate, review = item["candidate"], item["review"]
            decision = validate_bound_review(product, candidate, review)
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
        if not version_change and not set(previous["approved_sha256"]).issubset(approved):
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
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return summary
