"""私有更新最终验收登记：逐项核对真实证据和授权改动，再更新受影响基线。"""

import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
Q="validation/updates/"


def read(p): return json.loads((ROOT/p).read_text(encoding="utf-8"))
def sha(p): return hashlib.sha256((ROOT/p).read_bytes()).hexdigest()


def main():
    runtime=read(Q+"U_H/runtime_021/result.json"); gui=read(Q+"U_H/actual_gui_021/smoke.json")
    portable=read(Q+"U_H/portable_021/result.json"); replacement=read(Q+"U_PROGRAM_021/result.json")
    remote=read(Q+"published_client_021/result.json"); package=read(Q+"U_H/release_021/release.json")
    checks=read("validation/integration/code_checks.json")
    assert runtime["status"]==portable["status"]==replacement["status"]==remote["status"]=="verified"
    assert checks["success"] and checks["tests_run"]>=108
    assert runtime["self_test"]["app_version"]==gui["app_version"]=="0.2.1" and gui["private_update_controls"]
    assert gui["current_counts"]=={"1083284353580":10,"1084307568457":1} and not gui["generation_started"]
    assert "私有规则已同步" in gui["update_status"]
    assert package["sha256"]==remote["archive_sha256"]==portable["archive_sha256"]==sha(package["archive"])
    assert package["executable_sha256"]==runtime["executable_sha256"]==replacement["real_new_executable_sha256"]
    build=read(Q+"U_H/build_021/build_manifest.json")
    for p,value in build["source_sha256"].items(): assert sha(p)==value, "构建后的源码发生变化："+p
    manifest=ROOT/"validation/frozen_modules.json"; previous=manifest.read_bytes(); data=json.loads(previous)
    allowed={"run_pipeline.py","run_updates.py","pyproject.toml","tools/build_windows.py","tests/test_private_updates.py",
        *[f"src/soutui_image/{n}.py" for n in ("desktop_app","pipeline","quality_upgrade","review_button_edges",
          "review_detail_locator","review_group_state","review_record_validation","review_revision","stage_d_web_generation",
          "stage_e_codex_review","stage_f_output_revision","stage_f_outputs","rule_store","update_config")]}
    for name,module in data["modules"].items():
        for p,value in module["files"].items():
            if sha(p)!=value and p not in allowed: raise ValueError("非授权冻结文件变化："+p)
    history=ROOT/(Q+"final_frozen_before.json"); assert not history.exists(); history.write_bytes(previous)
    evidence=[Q+"U_H/gui_resume/result.json",Q+"U_H/runtime_021/result.json",Q+"U_H/actual_gui_021/smoke.json",
              Q+"U_H/portable_021/result.json",Q+"U_PROGRAM_021/result.json",Q+"published_client_021/result.json"]
    revised=[]
    for name,module in data["modules"].items():
        changed={p:sha(p) for p,value in module["files"].items() if sha(p)!=value}
        if not changed and module.get("status")!="requires_regression": continue
        module.setdefault("revisions",[]).append({"reason":"用户明确允许私有规则自动更新接入及重新验收；保留独立复核、600秒和三轮补生",
            "previous_files":dict(module["files"]),"evidence":evidence,"prior_baseline":str(history.relative_to(ROOT))})
        module["files"].update(changed); module["status"]="verified"; module.pop("pending_reason",None)
        module["evidence"]=list(dict.fromkeys(module["evidence"]+evidence)); revised.append(name)
        if name=="B": module["revision_limits"]="仅pyproject版本和签名依赖声明变化，B源码字节不变；依赖打包与exe导入通过，本轮未重复网站上传验证"
    h_files={"run_pipeline.py","pyproject.toml","tools/build_windows.py","tools/package_release.py",
        "tests/test_program_updates.py","tools/validate_program_replacement.py","tools/validate_published_private_release.py",
        "tools/private_update_panel.py","tools/register_private_release.py",
        *[f"src/soutui_image/{n}.py" for n in ("desktop_app","update_service","update_dialog","program_update","update_config",
          "github_updates","update_security","rule_store","rule_runtime","rules_bundle")],
        "src/soutui_image/resources/update_source.json","src/soutui_image/resources/review_rules.json"}
    h=data["modules"]["H_DESKTOP"]; h["files"].update({p:sha(p) for p in h_files})
    h["scope"]="本机私有规则下载、GUI复核暂停续跑、exe真实历史副本复核、ZIP迁移及新旧真实程序目录替换；任务规则固定"
    h["limits"]="另一台电脑及其只读令牌首次配置尚未实测；本轮没有新图生成或新模型审核，视觉质量标准未改变"
    data["modules"]["U_PRIVATE_CLIENT"]={"status":"verified","scope":h["scope"],"limits":h["limits"],"files":{p:sha(p) for p in sorted(h_files)},"evidence":evidence}
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    result={"status":"verified_scopes","completed_at":datetime.now(timezone.utc).isoformat(),"program_version":"0.2.1",
        "repository":"https://github.com/wanghua361/soutui-image-updates","private":True,"release":remote["release_url"],
        "archive_sha256":package["sha256"],"executable_sha256":package["executable_sha256"],"tests":checks["tests_run"],
        "revised_modules":revised,"evidence":evidence,"other_computer_verified":False,"new_cloud_image_calls":0,
        "rules_text_and_review_standards_unchanged":True,"existing_saved_counts":[10,1],"files":data["modules"]["U_PRIVATE_CLIENT"]["files"]}
    (ROOT/(Q+"release_acceptance.json")).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    with (ROOT/"docs/模块验收记录.md").open("a",encoding="utf-8") as handle:
        handle.write("\n\n## 私有更新0.2.1最终接入验收\n\n用户明确允许修改D/F/G/H及相关依赖并重新验收。私有仓库已创建，真实规则和程序Release已发布且可由GET客户端下载、验签及核对哈希。旧任务固定版本、新任务选新稳定版，损坏包或篡改签名被拒绝，不兼容规则保留原版并提供程序升级，已撤销规则阻断新任务。108项检查通过。主程序实际目录显示10/1张、组09原三轮上限暂停；源码GUI暂停继续、exe真实历史副本复核、本机ZIP迁移和真实新旧程序目录替换通过，旧程序备份和安装目录内记录保留。\n\n本轮审核文字和质量标准不变，没有新调用模型。测试读取私有发布使用维护机授权，其他电脑首次只读令牌及完整业务尚未现场验收。B仅依赖/版本声明变化，B源码不变，未将本次打包测试描述为再次网站上传验收。旧基线及源码保留于validation/history/before_private_updates_20261029与validation/updates各阶段目录；最终证据见validation/updates/release_acceptance.json。\n\n| 文件 | 实际SHA-256 |\n| --- | --- |\n")
        for f,v in result["files"].items(): handle.write(f"| `{f}` | `{v}` |\n")
    print(json.dumps({k:v for k,v in result.items() if k not in ("files","evidence")},ensure_ascii=True))


if __name__=="__main__": main()
