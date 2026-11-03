"""E 旧通过图复核入口：默认只列计划；--run 才实际审核，绝不提交生图或移动最终照片。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.review_revision import plan_revision, execute_revision
from soutui_image.stage_a_inputs import scan_inputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    batch = scan_inputs(args.input)
    if not batch.ready:
        raise ValueError("原图扫描未通过")
    plan = plan_revision(batch.products, args.data / "tasks", args.data / "review_cache")
    if not args.run:
        print(json.dumps({"candidate_count": plan["candidate_count"], "policy_version": plan["policy_version"],
                          "scope": "仅计划，未调用模型、未生图、未改最终输出"}, ensure_ascii=False))
        return 0
    calibration = json.loads((args.calibration / "result.json").read_text(encoding="utf-8"))
    if calibration["status"] not in ("verified", "verified_scope") or calibration["policy_version"] != plan["policy_version"]:
        raise ValueError("新规则实际图片校准未通过，不能开始下一步旧输出复核")
    if calibration.get("model", "default") != (args.model or "default"):
        raise ValueError("旧图复核必须使用实际校准过的同一个模型")
    result = execute_revision(batch.products, plan, args.output, args.data / "reviews",
                              model=args.model,
                              reuse_directories=[args.calibration / "reviews"],
                              on_event=lambda event: print(json.dumps(event, ensure_ascii=True), flush=True))
    print(json.dumps({k: v for k, v in result.items() if k != "reports"}, ensure_ascii=True), flush=True)
    return 0 if result["status"] == "staged" else 2


if __name__ == "__main__":
    raise SystemExit(main())
