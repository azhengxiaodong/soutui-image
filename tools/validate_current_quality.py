"""用用户标注的真实原图和结果图核验新规则；期望标签不传入模型提示词。"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image
from soutui_image.review_checkpoints import digest
from soutui_image.review_images import ReviewImage
from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_e_codex_review import review_candidate
from soutui_image.rules_bundle import builtin_bundle


def main():
    """实际执行隔离样本审核，保存逐张标签对照、用时与原始模型证据，不生成网站图片。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout", action="store_true", help="另用未参与提示词调整的历史人工正反例回归")
    parser.add_argument("--sample", help="只续验指定样本，多个名称用逗号分隔；复用已核验阶段断点")
    args = parser.parse_args()
    target = ROOT / "validation/current_quality"
    if args.holdout:
        manifest = {"samples": json.loads((target / "holdout/manifest.json").read_text(encoding="utf-8"))}
        for sample in manifest["samples"]:
            sample["name"] = "holdout_" + str(sample["sample"])
        target = target / "holdout"
        product = None
    else:
        manifest = json.loads((target / "cases.json").read_text(encoding="utf-8"))
        source = Path(manifest["source"])
        with Image.open(source) as image:
            product = ProductInput(manifest["product_id"], str(source), *image.size, image.format, digest(source))
    bundle = builtin_bundle()

    def worker(sample):
        start = time.perf_counter()
        try:
            current = ProductInput(**sample["product"]) if args.holdout else product
            result = review_candidate(current, ReviewImage(**sample["candidate"]), target / "reviews", model=bundle.model)
            return {"name": sample["name"], "expected": sample["expected"], "actual": result.decision,
                    "matched": result.decision == sample["expected"], "seconds": time.perf_counter() - start,
                    "review": asdict(result)}
        except Exception as error:
            return {"name": sample["name"], "expected": sample["expected"], "actual": "error", "matched": False,
                    "error": str(error), "seconds": time.perf_counter() - start}

    requested = set(args.sample.split(",")) if args.sample else None
    samples = [s for s in manifest["samples"] if requested is None or s["name"] in requested]
    if not samples:
        raise ValueError("没有对应的样本")
    suffix = "_" + args.sample.replace(",", "_") if args.sample else ""
    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in as_completed([executor.submit(worker, sample) for sample in samples]):
            result = future.result()
            results.append(result)
            (target / ("progress" + suffix + ".json")).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k != "review"}, ensure_ascii=True), flush=True)
    report = {"status": "verified_samples" if all(r["matched"] for r in results) else "needs_review",
              "policy_version": bundle.version, "bundle_sha256": bundle.sha256, "model": bundle.model,
              "results": results, "new_generation_submissions": 0,
              "scope": "未参与本轮调参的两张历史人工正反例" if args.holdout else "用户三张校准样本上的实际审核；不代表独立测试集或所有商品准确率保证"}
    (target / ("result" + suffix + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "verified_samples" else 2


if __name__ == "__main__":
    raise SystemExit(main())
