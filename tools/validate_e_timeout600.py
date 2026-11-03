"""用户授权后独立回归 E：新等待上限下，用真实原图与一正一反结果重新调用模型。"""

import inspect
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.review_images import ReviewImage
from soutui_image.stage_e_codex_review import _audit_once, review_candidate


if __name__ == "__main__":
    assert inspect.signature(_audit_once).parameters["timeout"].default == 600
    report = json.loads((ROOT / "data/review_cache/1083284353580/group_01/round_00/round_review.json").read_text(encoding="utf-8"))
    plan = json.loads((ROOT / "data/tasks/product_1083284353580_references.json").read_text(encoding="utf-8"))
    product = next(p for p in scan_inputs(Path(plan["source_path"]).parent).products if p.product_id == report["product_id"])
    target = ROOT / "validation" / "stage_e_timeout600"
    target.mkdir(parents=True, exist_ok=True)
    results = []
    # 期望标签仅在回归断言中使用，不传入模型提示词。
    for slot, expected in (("slot-1", "pass"), ("slot-0", "reject")):
        outcome = next(o for o in report["outcomes"] if o["candidate"]["result_id"].endswith("/" + slot))
        candidate = ReviewImage(**outcome["candidate"])
        start = time.monotonic()
        review = review_candidate(product, candidate, target / "model_reviews")
        result = {"result_id": candidate.result_id, "expected": expected, "actual": review.decision,
                  "elapsed_seconds": round(time.monotonic() - start, 2), "review": asdict(review)}
        results.append(result)
        (target / "progress.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "review"}, ensure_ascii=True), flush=True)
    success = all(r["actual"] == r["expected"] for r in results)
    result = {"status": "verified" if success else "needs_review", "timeout_per_call_seconds": 600,
              "authorization": "用户于本对话明确同意延长并重新验收", "actual_images": 2,
              "model_called_again_in_separate_evidence_directory": True,
              "prompt_and_judgment_policy_unchanged": True, "results": results}
    (target / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=True), flush=True)
    raise SystemExit(0 if success else 2)
