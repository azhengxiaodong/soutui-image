"""用用户给定的五个正反例调用真实 Codex；每张独立结论，不向模型泄露期望标签。"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.review_images import ReviewImage
from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_e_codex_review import review_candidate


def worker(item):
    product = ProductInput(**item["product"])
    candidate = ReviewImage(**item["candidate"])
    try:
        review = review_candidate(product, candidate, ROOT / "data" / "reviews")
        return {"sample": item["sample"], "expected": item["expected"], "actual": review.decision,
                "matched": item["expected"] == review.decision, "review": asdict(review)}
    except Exception as error:
        return {"sample": item["sample"], "expected": item["expected"], "actual": "error", "matched": False, "error": str(error)}


def main():
    items = json.loads((ROOT / "data" / "evaluation" / "user_samples" / "manifest.json").read_text(encoding="utf-8"))
    results = []
    target = ROOT / "validation" / "stage_e"
    target.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, item) for item in items]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({k: result[k] for k in ("sample", "expected", "actual", "matched")}, ensure_ascii=True), flush=True)
            (target / "user_sample_results.json").write_text(json.dumps(sorted(results, key=lambda r: r["sample"]), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["matched"] for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
