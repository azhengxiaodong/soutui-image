"""真实35张升级及本机交付验证全部通过后登记受影响板块；保留旧基线和限定范围。"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
Q="validation/quality_v2/"


def read(path): return json.loads((ROOT/path).read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256((ROOT/path).read_bytes()).hexdigest()


def main():
    """实际计算哈希，不把旧生图验收或三个校准样本说成新规则普遍准确率。"""
    baseline_path=ROOT/"validation/frozen_modules.json"; data=read("validation/frozen_modules.json")
    batch=read(Q+"batch_verified/result.json")
    gui=read(Q+"h_gui_release/result.json")
    runtime=read(Q+"package_runtime_clean/result.json")
    actual_gui=read(Q+"package_actual_gui/smoke.json")
    portable=read(Q+"package_portable_clean/result.json")
    release=read(Q+"package_release_clean/release.json")
    suite=read(Q+"sol_suite_01/result.json")
    resume=read(Q+"review_resume_live/result.json")
    prefix=read(Q+"g_prefix_live/result.json")
    checks=read("validation/integration/code_checks.json")
    assert batch["status"]=="verified" and batch["old_passes_rechecked"]==35 and batch["legacy_nonpass_kept_unchanged"]==54
    assert batch["new_generation_submissions"]==0 and not batch["rounds_reset"]
    assert suite["status"]=="verified_scope" and all(c["passed"] for c in suite["cases"])
    assert resume["status"]==prefix["status"]==gui["status"]==runtime["status"]==portable["status"]=="verified"
    assert gui["results"]==["paused","complete"] and prefix["next_round"]==2
    assert checks["success"] and checks["tests_run"]>=98
    assert actual_gui["packaged"] and not actual_gui["generation_started"]
    assert actual_gui["current_counts"]=={p["product_id"]:p["saved"] for p in batch["products"]}
    assert sha(release["archive"])==release["sha256"] and runtime["executable_sha256"]==release["executable_sha256"]==portable["executable_sha256"]
    build=read(Q+"package_build_clean/build_manifest.json")
    for path,value in build["source_sha256"].items(): assert sha(path)==value, "构建后源码变化"
    allowed={"run_pipeline.py","src/soutui_image/desktop_app.py","src/soutui_image/pipeline.py",
             "src/soutui_image/review_group_state.py","src/soutui_image/stage_d_web_generation.py",
             "src/soutui_image/stage_e_codex_review.py","src/soutui_image/stage_f_outputs.py",
             "tools/build_windows.py","tools/package_release.py"}
    for name,module in data["modules"].items():
        for path,value in module["files"].items():
            if sha(path)!=value and path not in allowed: raise ValueError(f"非授权冻结文件变化：{name}：{path}")
    history=ROOT/"validation/history/frozen_before_quality_release_20261029.json"
    assert not history.exists(), "禁止覆盖本次旧基线"
    history.write_bytes(baseline_path.read_bytes())
    audit=[f"src/soutui_image/{n}.py" for n in ("stage_e_codex_review","review_images","review_detail_locator",
        "review_detail_views","review_button_edges","review_checkpoints","review_record_validation","codex_clothing")]
    bridge=["src/soutui_image/review_group_state.py","src/soutui_image/review_revision.py","src/soutui_image/quality_upgrade.py",
            "src/soutui_image/stage_f_output_revision.py"]
    new_files={
        "D":audit+["tests/test_quality_bridge.py","tools/validate_quality_batch.py"],
        "E":audit+bridge+["tests/test_quality_v2.py","tests/test_review_revision.py","tests/test_review_record_validation.py",
                          "tests/test_review_checkpoints.py","tests/test_quality_bridge.py","tools/validate_quality_v2.py",
                          "tools/validate_review_resume.py","tools/validate_quality_batch.py"],
        "F":audit+bridge+["tools/validate_quality_batch.py"],
        "G_PIPELINE":audit+bridge+["tools/validate_quality_prefix.py","tools/validate_quality_batch.py","tools/register_quality_release.py"],
        "H_DESKTOP":audit+bridge+["tools/validate_quality_prefix.py","tools/validate_gui_quality.py","tools/validate_package_quality.py",
                                  "tools/validate_portable_quality.py","tools/register_quality_release.py"]}
    scope={
        "D":"旧规则计数门禁及原真实轮次只读回归，保留三轮上限；本次未新增网站提交",
        "E":"Sol三张用户样图整图校准，35张旧通过真实新版审核及阶段断点续跑；非独立准确率测试",
        "F":"真实35张旧图按原字节留存、新版通过逐张恢复、九组映射与去重",
        "G_PIPELINE":"35张旧通过升级、54张旧非通过证据保留、原任务及补生次数不改；前缀重复执行不新生图",
        "H_DESKTOP":"源码GUI复核暂停继续、新exe历史副本复核及窗口、本机ZIP解压迁移；Sol与规则显示一致"}
    evidence={
        "D":[Q+"batch_verified/result.json",Q+"g_prefix_live/result.json"],
        "E":[Q+"sol_suite_01/result.json",Q+"review_resume_live/result.json",Q+"batch_verified/result.json"],
        "F":[Q+"batch_verified/result.json"],
        "G_PIPELINE":[Q+"g_prefix_live/result.json",Q+"batch_verified/result.json"],
        "H_DESKTOP":[Q+"h_gui_release/result.json",Q+"package_runtime_clean/result.json",Q+"package_actual_gui/smoke.json",Q+"package_portable_clean/result.json",Q+"package_release_clean/release.json"]}
    now=datetime.now(timezone.utc).isoformat(); records={}
    for name,additions in new_files.items():
        module=data["modules"][name]; previous=dict(module["files"])
        module.setdefault("revisions",[]).append({"date":now,"reason":"用户要求修正袖口、门襟扣位及裤长漏判并更新实际软件包",
            "authorization":"用户要求处理好并继续，延续既有Sol审核；最后7张及对应原图/裁剪另获明确云端Sol外发授权。Astra不用于35张批量",
            "previous_files":previous,"prior_baseline":str(history.relative_to(ROOT)),"evidence":evidence[name]})
        module["files"]={path:sha(path) for path in sorted(set(previous)|set(additions))}
        module["status"]="verified"; module["accepted_at"]=now; module["scope"]=scope[name]
        module["limits"]="本次不新增生图。另一台真实电脑及两商品均完整九组未验收。模型仍可能漏判或误判，细微扣位独立诊断未稳定。"
        module["evidence"]=list(dict.fromkeys(module["evidence"]+evidence[name]+["validation/integration/code_checks.json"]))
        module.pop("pending_reason",None)
        if name=="E":
            module["quality_scope"]={"policy_version":"2026-10-29.5","model":"gpt-6.1-sol","previous_35_outputs_rechecked":True,
                "button_only_detection_verified":False,"scope":scope[name],"limits":module["limits"]}
        if name=="F": module["revision_scope"].update(user_original_outputs_moved=True,whole_reaudit_completed=True,scope=scope[name])
        records[name]={"scope":scope[name],"files":module["files"],"evidence":evidence[name]}
    temporary=baseline_path.with_suffix(".pending.json")
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); temporary.replace(baseline_path)
    acceptance={"status":"verified_scopes","accepted_at":now,"modules":records,"batch_result":batch,"release":release,
                "prior_baseline":str(history.relative_to(ROOT)),"other_computer_verified":False}
    (ROOT/(Q+"release_acceptance.json")).write_text(json.dumps(acceptance,ensure_ascii=False,indent=2),encoding="utf-8")
    with (ROOT/"docs/模块验收记录.md").open("a",encoding="utf-8") as handle:
        handle.write(f"\n\n## Sol质量升级与新版交付（{now}）\n\n用户授权修正漏判及必要交接，本次没有新增生图。旧基线原字节保留于 `{history.relative_to(ROOT)}`。\n\n35张旧通过逐张使用当前真实Sol审核；54张旧非通过原十项证据原样保留，不能说89张均新版重审。新版结果为{dict(batch['current_decisions'])}。仅通过图片回原九组目录，35张旧图原字节全部保留在本机待复核区；原任务文件和补生轮次不变。\n\n三个用户样图仅用于整图校准；独立细微扣位诊断仍不稳定，非准确率或零漏判保证。本次不重复生图，另一台电脑及两商品均完整九组仍未验收。\n")
        for name,record in records.items():
            handle.write(f"\n### {name}本次范围\n\n{record['scope']}。复现入口及证据：{', '.join(record['evidence'])}。98项规则/控制检查通过。\n\n| 文件 | 实际SHA-256 |\n| --- | --- |\n")
            for path,value in record["files"].items(): handle.write(f"| `{path}` | `{value}` |\n")
    print(json.dumps({"status":"verified_scopes","modules":list(records),"batch_decisions":batch["current_decisions"],"release_sha256":release["sha256"]},ensure_ascii=True))


if __name__=="__main__": main()
