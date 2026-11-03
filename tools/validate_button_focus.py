"""用同样两张错误图和原认可正例校准扣位专项；不会改 E 正式规则、原记录或最终输出。"""

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.review_button_edges import review_button_edges
from validate_quality_v2 import samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    manifests = list(args.calibration.glob("reviews/*/*/first/detail_views/manifest.json"))
    work = []
    for item in samples():
        matching = []
        for manifest in manifests:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if any(v["role"] == "generated" and v["source_sha256"] == item["candidate"].sha256 for v in data["views"]):
                matching.append(data["views"])
        if len(matching) != 1:
            raise ValueError("该样图没有唯一的真实定位局部证据")
        work.append((item, matching[0]))
    def worker(item, views):
        try:
            neutral = hashlib.sha256((item["product"].sha256 + item["candidate"].sha256).encode()).hexdigest()[:24]
            check = review_button_edges(item["product"], item["candidate"], views, root / neutral, model=args.model)
            return {"name": item["name"], "check": check}
        except Exception as error:
            return {"name": item["name"], "error": str(error)}
    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in as_completed([executor.submit(worker, item, views) for item, views in work]):
            result = future.result(); results.append(result)
            (root / "progress.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=True), flush=True)
    record = {"model": args.model, "results": results, "status": "completed_not_accepted", "formal_audit_changed": False}
    (root / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
