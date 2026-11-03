"""使用同款实际白底文件复验认可局部图，排除网页截图放大产生的原图细节模糊。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.review_detail_locator import validate_regions
from soutui_image.review_images import inspect_image
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_e_codex_review import POLICY_VERSION, _audit_once


def main():
    """原图与正例确属用户展示的同款；新调用不读取认可标签，只比较真实图像。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    target = args.output.resolve(); target.mkdir(parents=True, exist_ok=False)
    product = next(p for p in scan_inputs(r"C:\Users\王\Desktop\家居服白底").products if p.product_id == "1083284353580")
    candidate = inspect_image(product.product_id, "calibration-sample-03-canonical-source", ROOT / "data/evaluation/user_samples/sample_3/result.png")
    located = []
    origins = []
    # 仅复用当前同一张图片的真实定位框：原图框来自原始800px文件，结果框来自同一827px正例。
    for role, expected in (("original", product.sha256), ("generated", candidate.sha256)):
        matches = []
        for path in args.comparison.glob("reviews/*/*/detail_location/located.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            key = "source_sha256" if role == "original" else "result_sha256"
            if data[key] == expected and data["model"] == args.model:
                matches.append((path, data))
        if not matches:
            raise ValueError("没有该原始图片的真实定位证据")
        path, data = matches[0]
        located.extend(r for r in data["regions"] if r["role"] == role)
        origins.append({"role": role, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    located = validate_regions({"regions": located})
    work = target / "actual_run"
    location = work / "detail_location"; location.mkdir(parents=True)
    (location / "located.json").write_text(json.dumps({"source_sha256": product.sha256, "result_sha256": candidate.sha256,
        "model": args.model, "regions": located, "reused_coordinate_sources": origins}, ensure_ascii=False, indent=2), encoding="utf-8")
    record = {"policy_version": POLICY_VERSION, "model": args.model, "source_path": product.source_path,
              "source_sha256": product.sha256, "candidate_sha256": candidate.sha256,
              "scope": "用户认可正例与同一件服装的真实原图；未把期望标签传给模型，定位复用不含合格结论",
              "status": "running", "passes": []}
    (target / "progress.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    for verification in (False, True):
        result = _audit_once(product, candidate, work / ("verification" if verification else "first"),
                             verification=verification, model=args.model)
        record["passes"].append(result)
        (target / "progress.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"verification": verification, "decision": result["decision"]}, ensure_ascii=True), flush=True)
        if result["decision"] != "pass":
            break
    record["status"] = "verified_positive" if len(record["passes"]) == 2 and all(r["decision"] == "pass" for r in record["passes"]) else "needs_review"
    files = ["src/soutui_image/stage_e_codex_review.py", "src/soutui_image/review_detail_locator.py",
             "src/soutui_image/review_detail_views.py", "src/soutui_image/review_button_edges.py"]
    record["files"] = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files}
    (target / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(record["status"], flush=True)
    return 0 if record["status"] == "verified_positive" else 2


if __name__ == "__main__":
    raise SystemExit(main())
