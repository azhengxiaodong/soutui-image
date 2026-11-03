"""新细节规则独立实测：用户指出的两张漏判原尺寸图及原认可的局部正例。"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.review_images import ReviewImage
from soutui_image.stage_a_inputs import ProductInput, scan_inputs
from soutui_image.stage_e_codex_review import POLICY_VERSION, review_candidate


def samples():
    """绑定原图和真实结果；期望标签仅用于验收，不进入模型提示词。"""
    product = next(p for p in scan_inputs(r"C:\Users\王\Desktop\家居服白底").products if p.product_id == "1083284353580")
    report = json.loads((ROOT / "data/review_cache/1083284353580/group_06/round_01/round_review.json").read_text(encoding="utf-8"))
    entries = []
    for prefix, name, features in (
        ("63a3c25f0ab7", "user_new_cuff_placket_buttons", ["袖口边缘结构", "门襟层次与边线", "扣位边缘与凸片"]),
        ("ff83c197ceaf", "user_new_short_trousers", ["裤长与脚踝位置"]),
    ):
        item = next(o for o in report["outcomes"] if o["candidate"]["sha256"].startswith(prefix))
        entries.append({"name": name, "expected": "reject", "required_mismatches": features,
                        "product": product, "candidate": ReviewImage(**item["candidate"])})
    old = json.loads((ROOT / "data/evaluation/user_samples/manifest.json").read_text(encoding="utf-8"))
    positive = next(i for i in old if i["sample"] == 3)
    entries.append({"name": "user_approved_cropped_positive", "expected": "pass", "required_mismatches": [],
                    "product": ProductInput(**positive["product"]), "candidate": ReviewImage(**positive["candidate"])})
    return entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=False)
    def worker(item):
        try:
            result = review_candidate(item["product"], item["candidate"], target / "reviews", model=args.model)
            detected = sorted({c["feature"] for p in result.passes for c in p["checks"] if c["comparison"] == "mismatch"})
            return {"name": item["name"], "expected": item["expected"], "actual": result.decision,
                    "decision_matched": result.decision == item["expected"], "required_mismatches": item["required_mismatches"],
                    "detected_mismatches": detected, "all_requested_details_detected": set(item["required_mismatches"]) <= set(detected),
                    "review": asdict(result)}
        except Exception as error:
            return {"name": item["name"], "expected": item["expected"], "actual": "error", "error": str(error),
                    "decision_matched": False, "all_requested_details_detected": False}
    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in as_completed([executor.submit(worker, item) for item in samples()]):
            result = future.result(); results.append(result)
            (target / "progress.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k != "review"}, ensure_ascii=True), flush=True)
    record = {"policy_version": POLICY_VERSION, "model": args.model or "default", "status": "verified" if all(
        r["decision_matched"] and r["all_requested_details_detected"] for r in results) else "needs_review", "results": results,
        "scope": "用户规则校准，非独立准确率测试；旧正例不能因未展示裤脚被淘汰"}
    (target / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in record.items() if k != "results"}, ensure_ascii=True), flush=True)
    return 0 if record["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
