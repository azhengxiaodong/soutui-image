"""仅在真实证据全部通过后首次登记 G/H；拒绝覆盖旧基线或掩盖旧文件变化。"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def sha(relative):
    with (ROOT / relative).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


if __name__ == "__main__":
    baseline = read("validation/frozen_modules.json")
    assert "G_PIPELINE" not in baseline["modules"] and "H_DESKTOP" not in baseline["modules"]
    assert all(sha(p) == expected for m in baseline["modules"].values() for p, expected in m["files"].items())
    full = read("validation/integration/full_product_after_multi.json")
    multi = read("validation/integration/multi_product_row01.json")
    gui = read("validation/gui_resume/result.json")
    package = read("validation/package_final/self_test_result.json")
    resume = read("validation/package_final/cached_pipeline.json")
    relocated = read("validation/package_final/relocated.json")
    release = read("validation/package_final/release.json")
    assert full["status"] == "verified" and full["saved"] == 32 and full["reviewed"] == 84
    assert multi["status"] == "verified" and multi["first_product_new_submissions"] == 0
    assert gui["results"] == ["paused", "complete"] and gui["new_generation_submissions"] == 0
    assert gui["job_files_unchanged"] and gui["approved_images_unchanged"]
    assert package["status"] == "ok" and package["audit_timeout_per_call_seconds"] == 600
    assert resume["actual_full_nine_group_resume"] and resume["new_generation_submissions"] == 0
    assert relocated["exit_code"] == 0 and relocated["self_test"]["audit_timeout_per_call_seconds"] == 600
    assert sha("release/搜推生图-Windows测试版.zip") == release["sha256"]
    common = [f"src/soutui_image/{name}.py" for name in (
        "__init__", "stage_a_inputs", "codex_clothing", "stage_b_web_upload", "stage_c_web_plan",
        "stage_d_web_generation", "stage_e_codex_review", "stage_f_outputs", "review_images",
        "review_group_state", "web_session", "web_transport")]
    runtime = ["src/soutui_image/pipeline.py", "src/soutui_image/runtime_lock.py", "run_pipeline.py", "main.py"]
    g_files = runtime + common + ["tests/test_pipeline_controls.py", "tools/validate_integration.py",
        "tools/validate_multi_product.py", "tools/run_checks.py", "tools/freeze_g_h.py"]
    h_files = runtime + common + list(baseline["modules"]["DESKTOP_CONNECT"]["files"]) + [
        "src/soutui_image/desktop_app.py", "src/soutui_image/window_capture.py", "tools/validate_gui_resume.py",
        "tools/build_windows.py", "tools/package_release.py", "requirements-build.txt", "pyproject.toml"]
    history = ROOT / "validation/history/frozen_before_G_H_20261028.json"
    assert not history.exists()
    history.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    accepted = datetime.now(timezone.utc).isoformat()
    baseline["modules"]["G_PIPELINE"] = {
        "status": "verified", "accepted_at": accepted,
        "scope": "一商品完整九组及两商品第一组串联；暂停、断点续跑、只保存双次通过及不重复提交",
        "limits": "未执行两商品均全九组；真实三轮仍不足的暂停场景未触发，上限规则由检查覆盖",
        "evidence": ["validation/integration/full_product_after_multi.json", "validation/integration/multi_product_row01.json",
                     "validation/integration/code_checks.json", "validation/pipeline_error_check/result.json"],
        "files": {p: sha(p) for p in sorted(set(g_files))}}
    baseline["modules"]["H_DESKTOP"] = {
        "status": "verified", "accepted_at": accepted,
        "scope": "本机源码 GUI 开始/暂停/继续，exe 窗口与实际九组续跑，ZIP 解压迁移和 600 秒配置",
        "limits": "另一台电脑未实测；早期 tools/smoke_gui.py 不作为最终截图验收入口",
        "evidence": ["validation/gui_resume/result.json", "validation/package_final/self_test_result.json",
                     "validation/package_final/gui/smoke.json", "validation/package_final/cached_pipeline.json",
                     "validation/package_final/relocated.json", "validation/package_final/release.json",
                     "validation/package_final/runtime_versions.json"],
        "files": {p: sha(p) for p in sorted(set(h_files))}}
    temporary = ROOT / "validation/frozen_modules.pending.json"
    temporary.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(ROOT / "validation/frozen_modules.json")
    result = {"new_frozen_modules": ["G_PIPELINE", "H_DESKTOP"], "accepted_at": accepted,
              "existing_module_baselines_preserved": True, "other_computer_verified": False,
              "source_file_counts": {name: len(baseline["modules"][name]["files"]) for name in ("G_PIPELINE", "H_DESKTOP")}}
    (ROOT / "validation/integration/acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
