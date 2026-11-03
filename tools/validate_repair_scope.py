"""E 修正验收：中性环境独立检查扣位反例，结合真实原文件正例与同输入的首次审核。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.review_button_edges import review_button_edges
from soutui_image.stage_e_codex_review import POLICY_VERSION, REVIEW_SCHEMA, _prompt, validate_review
from validate_quality_v2 import samples


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def first_evidence(sample, comparison, model, *, require_current_prompt=True):
    """首审提示词、schema、图像和真实模型均须一致，才可复用未改的立即拒绝路径。"""
    matches = []
    for path in comparison.glob("reviews/*/*/first/detail_views/manifest.json"):
        views = json.loads(path.read_text(encoding="utf-8"))["views"]
        if any(v["role"] == "generated" and v["source_sha256"] == sample["candidate"].sha256 for v in views):
            matches.append((path, views))
    if len(matches) != 1:
        raise ValueError("首审证据不是唯一对应当前图")
    path, views = matches[0]
    folder = path.parent.parent
    if any(word in str(folder).lower() for word in ("positive", "negative", "approved", "reject")):
        raise ValueError("模型工作目录含预期结论提示，不能验收")
    log = (folder / "stderr.txt").read_text(encoding="utf-8")
    if ((require_current_prompt and _prompt(sample["product"], sample["candidate"], views=views).strip() not in log)
            or f"model: {model}" not in log):
        raise ValueError("首审实际提示词或模型不同")
    if json.loads((folder / "schema.json").read_text(encoding="utf-8")) != REVIEW_SCHEMA:
        raise ValueError("首审检查项不同")
    verdict = validate_review(json.loads((folder / "response.json").read_text(encoding="utf-8")),
                              sample["product"].product_id, sample["candidate"].result_id)
    for view in views:
        expected = sample["product"].sha256 if view["role"] == "original" else sample["candidate"].sha256
        if view["source_sha256"] != expected or hashlib.sha256(Path(view["path"]).read_bytes()).hexdigest() != view["sha256"]:
            raise ValueError("局部图片与实际原图/结果不符")
    return verdict, views, str(folder / "response.json")


def main():
    """--run-component 只做新的真实专项；--finalize 在全部实证齐全后判断是否通过。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--run-component", action="store_true")
    args = parser.parse_args()
    target = args.output.resolve()
    if any(w in str(target).lower() for w in ("positive", "negative", "approved", "reject")):
        raise ValueError("验收目录必须使用中性名称")
    target.mkdir(parents=True, exist_ok=True)
    cases = samples()
    first, views, first_path = first_evidence(cases[0], args.comparison, args.model, require_current_prompt=not args.run_component)
    if args.run_component:
        check = review_button_edges(cases[0]["product"], cases[0]["candidate"], views, target / "case_01", model=args.model)
        write(target / "component.json", {"policy_version": POLICY_VERSION, "model": args.model,
            "source_sha256": cases[0]["product"].sha256, "candidate_sha256": cases[0]["candidate"].sha256,
            "check": check, "evidence": str(target / "case_01")})
        print(json.dumps(check, ensure_ascii=True), flush=True)
        return 0
    component = json.loads((target / "component.json").read_text(encoding="utf-8"))
    normal = json.loads((args.canonical / "result.json").read_text(encoding="utf-8"))
    if (component["policy_version"] != POLICY_VERSION or normal["policy_version"] != POLICY_VERSION
            or component["model"] != args.model or normal["model"] != args.model):
        raise ValueError("验收的协议/模型不相同")
    checks = {c["feature"]: c for c in first["checks"]}
    a = first["decision"] == "reject" and all(checks[f]["comparison"] == "mismatch" for f in ("袖口边缘结构", "门襟层次与边线"))
    a = a and component["check"]["comparison"] in ("mismatch", "uncertain")
    second, _, second_path = first_evidence(cases[1], args.comparison, args.model)
    length = next(c for c in second["checks"] if c["feature"] == "裤长与脚踝位置")
    b = second["decision"] == "reject" and length["visibility"] != "not_visible" and length["comparison"] in ("mismatch", "uncertain")
    c = normal["status"] == "verified_positive" and normal["source_sha256"] == cases[0]["product"].sha256
    c = c and normal["candidate_sha256"] == cases[2]["candidate"].sha256
    for verdict in normal["passes"]:
        validate_review(verdict, "1083284353580", "calibration-sample-03-canonical-source")
        length_check = next(x for x in verdict["checks"] if x["feature"] == "裤长与脚踝位置")
        c = c and verdict["decision"] == "pass" and length_check["visibility"] == "not_visible"
    files = ["src/soutui_image/stage_e_codex_review.py", "src/soutui_image/review_detail_locator.py",
             "src/soutui_image/review_detail_views.py", "src/soutui_image/review_button_edges.py"]
    if any(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != value for p, value in normal["files"].items()):
        raise ValueError("正例验证后的源码又被改变")
    result = {"policy_version": POLICY_VERSION, "model": args.model,
        "status": "verified_scope" if a and b and c else "needs_review",
        "cases": [{"case": 1, "passed": a, "first": first_path, "focus": component},
                  {"case": 2, "passed": b, "first": second_path, "length": length},
                  {"case": 3, "passed": c, "actual_full_protocol": str(args.canonical / "result.json")}],
        "files": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files},
        "limits": "同三张用户校准图，不是独立准确率测试；细微扣位和裁切后的裤长可保守待复核。正例用实际白底文件，非放大网页截图。",
        "previous_failed_experiments_preserved": True, "old_35_outputs_rechecked": False}
    write(target / "result.json", result)
    print(json.dumps({k:v for k,v in result.items() if k not in ("cases","files")}, ensure_ascii=True), flush=True)
    return 0 if result["status"] == "verified_scope" else 2


if __name__ == "__main__":
    raise SystemExit(main())
