"""仅登记 E 图片审核修正的实际验收范围；旧输出升级及 G/H 回归另行验收。"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(relative):
    """由实际文件计算 SHA-256，不从说明文档推断新基线。"""
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def main():
    """先核验真实模型证据和其他冻结文件，再保留旧基线、登记仅 E 的新范围。"""
    evidence = "validation/quality_v2/acceptance_policy5.json"
    actual = json.loads((ROOT / evidence).read_text(encoding="utf-8"))
    if actual["status"] != "verified_scope" or actual["model"] != "gpt-6-astra":
        raise ValueError("E 新规则没有完成实际验收，未替换基线")
    if any(sha(p) != value for p, value in actual["files"].items()):
        raise ValueError("模型验收后的源码又发生变化，未冻结")
    checks = json.loads((ROOT / "validation/integration/code_checks.json").read_text(encoding="utf-8"))
    if not checks["success"] or checks["tests_run"] < 86:
        raise ValueError("代码检查未完成")
    for extension in ("json", "log"):
        source = ROOT / f"validation/integration/code_checks.{extension}"
        destination = ROOT / f"validation/quality_v2/code_checks.{extension}"
        if destination.exists() and destination.read_bytes() != source.read_bytes():
            raise ValueError("本次 E 代码检查归档已经存在不同内容，未覆盖")
        if not destination.exists():
            destination.write_bytes(source.read_bytes())
    manifest = ROOT / "validation/frozen_modules.json"
    baseline = json.loads(manifest.read_text(encoding="utf-8"))
    allowed = "src/soutui_image/stage_e_codex_review.py"
    for module in baseline["modules"].values():
        for path, value in module["files"].items():
            if path != allowed and sha(path) != value:
                raise ValueError(f"发现其他冻结文件变化：{path}")
    history = "validation/history/frozen_before_E_quality5_20261029.json"
    history_path = ROOT / history
    if history_path.exists():
        raise FileExistsError("E 修正基线已经登记，拒绝覆盖历史")
    history_path.write_bytes(manifest.read_bytes())
    old = baseline["modules"]["E"]
    old_files = dict(old["files"])
    new_files = ["src/soutui_image/review_detail_locator.py", "src/soutui_image/review_detail_views.py",
                 "src/soutui_image/review_button_edges.py", "tests/test_quality_v2.py", "tools/validate_quality_v2.py",
                 "src/soutui_image/review_record_validation.py", "tests/test_review_record_validation.py",
                 "tools/validate_current_cases.py", "tools/validate_positive_original.py", "tools/accept_current_scope.py",
                 "tools/register_e_quality.py"]
    old["files"] = {p: sha(p) for p in sorted(set(old_files) | set(new_files))}
    old.setdefault("revisions", []).append({"date": "2026-10-29", "reason": "用户报告袖口、门襟、扣位和裤长漏判；增加定位局部、四项检查和扣位分段复核",
        "authorization": "用户的明确缺陷报告及三张样图的 Astra 对照审核授权；尚未扩展为整批 Astra 外发授权",
        "previous_files": old_files, "prior_baseline": history,
        "previous_source_snapshot": "validation/history/quality_v2_20261028/", "evidence": evidence})
    old["evidence"].append(evidence)
    old["evidence"].append("validation/quality_v2/code_checks.json")
    old["quality_scope"] = {"policy_version": actual["policy_version"], "model": actual["model"],
        "scope": "同三张用户校准图片：错误图不放行、原认可局部正例保留，拟通过者增加扣位分段复核",
        "limits": actual["limits"], "button_only_detection_verified": actual["button_only_detection_verified"],
        "default_model_switched": False, "previous_35_outputs_rechecked": False}
    for name in ("G_PIPELINE", "H_DESKTOP"):
        baseline["modules"][name]["status"] = "requires_regression"
        baseline["modules"][name]["pending_reason"] = "E 质量修正已独立验证；正式模型、旧合格输出升级及新版本交付仍需验收。文件旧基线保留。"
    temporary = manifest.with_suffix(".pending.json")
    temporary.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    record = {"registered_at": datetime.now(timezone.utc).isoformat(), "module": "E", "quality_scope": old["quality_scope"],
              "old_baseline": history, "files": old["files"], "other_frozen_sources_unchanged": True,
              "downstream_regression_pending": ["G_PIPELINE", "H_DESKTOP"], "new_generation_submissions": 0}
    (ROOT / "validation/quality_v2/E_acceptance.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    table = "\n".join(f"| `{p}` | `{value}` |" for p, value in old["files"].items())
    text = f"""\n\n## E 图片质量修正独立验收（2026-10-29，限定 Astra 变体）

- 用户报告的两张旧误留图在当前版本的新模型调用中均拒绝。原认可局部图改用同款实际白底原文件后，初审、独立整图复核及扣位专项均通过；未展示裤脚不影响通过。之前失败与带提示名称的校准记录全部保留，未用于这次验收。
- 本轮校准不是独立准确率测试。先前的普通双次判断仍漏掉细节，因此新协议拟通过者还要做扣位分段专项，任何错误或不确定均不保存。保留两次整图审核，单次等待仍600秒。
- 验证实际使用 `gpt-6-astra`；未切换正式默认模型，未将对照授权扩展成整批图片外发。原35张保存结果尚未新规则复核，不能继续称为当前合格数量。
- 本轮使用中性ID和目录，所有整图判定重新实际调用当前规则；只复用同图定位坐标，不复用旧判定。证据：`{evidence}`、`validation/quality_v2/cases_03/`、`canonical_source_03/`。86项规则/文件保护检查通过，不用它们代替真实图像验证。
- 验收限于三张样图的整图业务判定。单独细微扣位诊断仍未独立通过，错误样图包含袖口和门襟等多处差异，不能把整图拒绝说成每一处均能单独识别，也不能声称模型零漏判。
- 旧基线保存在 `{history}`，v1及v3源码历史均保留。仅登记 E 已实际验证的审核范围；旧输出迁移、G/H联调、新 Windows 包及其他电脑仍未验收。复核计划及迁移工具只实现/部分检查，不纳入已通过冻结范围。
- G/H共享 E 的旧基线保留，状态标为需回归，不用同步更新哈希掩盖尚未联调。其他冻结源码校验一致。统一入口不据此宣布整个修正完成。

| 文件 | 实际 SHA-256 |
| --- | --- |
{table}
"""
    with (ROOT / "docs/模块验收记录.md").open("a", encoding="utf-8") as handle:
        handle.write(text)
    print(json.dumps({k: v for k, v in record.items() if k != "files"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
