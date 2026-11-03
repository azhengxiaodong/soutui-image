"""当前规则反例实测：只复用同图真实定位坐标，不复用旧审核判断。"""

import argparse
import hashlib
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.stage_e_codex_review import POLICY_VERSION, _audit_once
from validate_quality_v2 import samples


def main():
    """两张真实错误图分别跑当前完整审核路径，输出新模型调用及源代码哈希。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    def worker(index, sample):
        target = root / f"case_{index:02}"
        target.mkdir()
        matches = []
        for p in args.comparison.glob("reviews/*/*/detail_location/located.json"):
            data = json.loads(p.read_text(encoding="utf-8"))
            if (data["source_sha256"] == sample["product"].sha256 and data["result_sha256"] == sample["candidate"].sha256
                    and data["model"] == args.model): matches.append(p.parent)
        if len(matches) != 1:
            raise ValueError("没有唯一对应当前原图、结果和模型的定位坐标")
        shutil.copytree(matches[0], target / "detail_location")
        passes = []
        for verification in (False, True):
            verdict = _audit_once(sample["product"], sample["candidate"], target / ("verification" if verification else "first"),
                                  verification=verification, model=args.model)
            passes.append(verdict)
            if verdict["decision"] != "pass": break
        return {"case": index, "policy_version": POLICY_VERSION, "model": args.model,
                "source_sha256": sample["product"].sha256, "candidate_sha256": sample["candidate"].sha256,
                "passes": passes, "decision": passes[-1]["decision"], "expected": "reject"}
    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in as_completed([executor.submit(worker, i, sample) for i, sample in enumerate(samples()[:2], 1)]):
            result = future.result(); results.append(result)
            (root / "progress.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case":result["case"],"decision":result["decision"]}, ensure_ascii=True), flush=True)
    files = ["src/soutui_image/stage_e_codex_review.py", "src/soutui_image/review_detail_locator.py",
             "src/soutui_image/review_detail_views.py", "src/soutui_image/review_button_edges.py"]
    record = {"policy_version": POLICY_VERSION, "model": args.model, "results": results,
              "status": "verified_cases" if all(r["decision"] == "reject" for r in results) else "needs_review",
              "files": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files}}
    (root / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if record["status"] == "verified_cases" else 2


if __name__ == "__main__":
    raise SystemExit(main())
