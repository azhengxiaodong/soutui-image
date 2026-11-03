"""E v4 真实回归：复用刚完成且输入完全相同的首审证据，实际执行新增扣位复核整条路径。"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.stage_e_codex_review import POLICY_VERSION, REVIEW_SCHEMA, _audit_once, _prompt, validate_review
from validate_quality_v2 import samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--focus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    target = args.output.resolve(); target.mkdir(parents=True, exist_ok=False)
    focused = json.loads((args.focus / "result.json").read_text(encoding="utf-8"))
    if focused["model"] != args.model:
        raise ValueError("扣位专项不是同一个模型")
    checks = {x["name"]: x["check"] for x in focused["results"]}
    cases = []
    positive = None
    for sample in samples():
        matches = []
        for path in args.comparison.glob("reviews/*/*/first/detail_views/manifest.json"):
            views = json.loads(path.read_text(encoding="utf-8"))["views"]
            if any(v["role"] == "generated" and v["source_sha256"] == sample["candidate"].sha256 for v in views):
                matches.append((path, views))
        if len(matches) != 1:
            raise ValueError("没有唯一的首审原像素证据")
        path, views = matches[0]
        first_dir = path.parent.parent
        schema = json.loads((first_dir / "schema.json").read_text(encoding="utf-8"))
        if schema != REVIEW_SCHEMA:
            raise ValueError("首审结构化规则不同，不能复用")
        prompt = _prompt(sample["product"], sample["candidate"], views=views)
        log = (first_dir / "stderr.txt").read_text(encoding="utf-8")
        if prompt.strip() not in log:
            raise ValueError("首审实际提示词与当前未变的首审路径不一致，不能复用")
        if f"model: {args.model}" not in log:
            raise ValueError("首审实际模型不符")
        first_path = first_dir / "response.json"
        first = validate_review(json.loads(first_path.read_text(encoding="utf-8")), sample["product"].product_id,
                                sample["candidate"].result_id)
        case = {"name": sample["name"], "first_decision": first["decision"],
                "first_actual_evidence": str(first_path), "first_sha256": hashlib.sha256(first_path.read_bytes()).hexdigest(),
                "prompt_identical": True, "button_focus": checks[sample["name"]]}
        if sample["expected"] == "reject":
            by_feature = {c["feature"]: c for c in first["checks"]}
            if sample["name"] == "user_new_cuff_placket_buttons":
                accepted = (first["decision"] == "reject" and
                            all(by_feature[f]["comparison"] == "mismatch" for f in ("袖口边缘结构", "门襟层次与边线")) and
                            case["button_focus"]["comparison"] in ("mismatch", "uncertain"))
            else:
                accepted = (first["decision"] == "reject" and by_feature["裤长与脚踝位置"]["visibility"] != "not_visible" and
                            by_feature["裤长与脚踝位置"]["comparison"] in ("mismatch", "uncertain"))
            case["passed"] = accepted
            case["length_check"] = by_feature["裤长与脚踝位置"]
        else:
            if first["decision"] != "pass":
                raise ValueError("原认可正例首审没有通过")
            positive = (sample, case, first_dir.parent / "detail_location")
        cases.append(case)
    if positive is None:
        raise ValueError("缺少原认可正例")
    sample, case, location = positive
    # 定位只提供坐标、不提供合格结论；保留真实模型和来源，避免再次浪费一次定位调用。
    work = target / "case_03"
    work.mkdir()
    shutil.copytree(location, work / "detail_location")
    (target / "progress.json").write_text(json.dumps({"state": "actual_verification_running", "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
    second = _audit_once(sample["product"], sample["candidate"], work / "verification", verification=True, model=args.model)
    case["new_actual_verification"] = second
    length = next(c for c in second["checks"] if c["feature"] == "裤长与脚踝位置")
    case["passed"] = second["decision"] == "pass" and length["visibility"] == "not_visible"
    files = ["src/soutui_image/stage_e_codex_review.py", "src/soutui_image/review_detail_locator.py",
             "src/soutui_image/review_detail_views.py", "src/soutui_image/review_button_edges.py"]
    result = {"policy_version": POLICY_VERSION, "model": args.model,
              "status": "verified_scope" if all(c["passed"] for c in cases) else "needs_review", "cases": cases,
              "files": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files},
              "limits": "同三张用户校准图，非独立准确率测试。第一张扣位及第二张裤长为保守不确定，未声称均可明确判错。",
              "verification_scope": "首审提示词/结构/图片完全一致的本轮 Astra 实证，加上新协议真实整图复核和真实扣位分段专项；旧输出未改"}
    (target / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("cases", "files")}, ensure_ascii=True), flush=True)
    return 0 if result["status"] == "verified_scope" else 2


if __name__ == "__main__":
    raise SystemExit(main())
