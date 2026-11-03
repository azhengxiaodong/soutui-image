"""登记当前版本实测的业务结果，单项诊断不足必须同时保留，不能当作全部缺陷定位成功。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.stage_e_codex_review import POLICY_VERSION, validate_review


def main():
    """三张均为当前版本的新调用；只验收已实测范围，不改写历史失败记录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = [json.loads(p.read_text(encoding="utf-8")) for p in
               (args.cases / "result.json", args.canonical / "result.json", args.component / "component.json")]
    negative, normal, focused = records
    for record in records:
        if record["policy_version"] != POLICY_VERSION or record["model"] != "gpt-6-astra":
            raise ValueError("验收模型或规则版本不符")
        for path, expected in record.get("files", {}).items():
            if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected:
                raise ValueError("模型验证后的文件又发生变化")
    negative_ok = len(negative["results"]) == 2 and all(r["decision"] == "reject" for r in negative["results"])
    normal_ok = normal["status"] == "verified_positive" and len(normal["passes"]) == 2
    for verdict in normal["passes"]:
        validate_review(verdict, "1083284353580", "calibration-sample-03-canonical-source")
        length = next(c for c in verdict["checks"] if c["feature"] == "裤长与脚踝位置")
        normal_ok = normal_ok and verdict["decision"] == "pass" and length["visibility"] == "not_visible"
    result = {"policy_version": POLICY_VERSION, "model": "gpt-6-astra",
              "status": "verified_scope" if negative_ok and normal_ok else "needs_review",
              "business_checks": {"both_error_images_rejected": negative_ok, "approved_partial_image_passed": normal_ok},
              "button_only_diagnostic": focused["check"],
              "button_only_detection_verified": focused["check"]["comparison"] in ("mismatch", "uncertain"),
              "evidence": [str(args.cases / "result.json"), str(args.canonical / "result.json"), str(args.component / "component.json")],
              "files": normal["files"],
              "limits": "仅同三张用户校准图的整图业务判定，不是准确率测试。反例含多处差异；单独细微扣位诊断结果另列，不能将整图拒绝说成所有细节均定位成功。未展示裤脚排除，展示但裤长无法定位则待复核。",
              "old_35_outputs_rechecked": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists(): raise FileExistsError("验收文件已存在，不能覆盖")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("files","button_only_diagnostic")}, ensure_ascii=True), flush=True)
    return 0 if result["status"] == "verified_scope" else 2


if __name__ == "__main__":
    raise SystemExit(main())
