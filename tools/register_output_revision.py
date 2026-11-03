"""登记 F 独立撤回验证范围，保留旧基线；真实整批重审及 G/H 联调另行验收。"""

import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256((ROOT/path).read_bytes()).hexdigest()


def main():
    """只在真实35张副本的字节/恢复验证通过后登记F，不把它当35张视觉复核完成。"""
    evidence="validation/quality_v2/f_io_live/result.json"
    result=json.loads((ROOT/evidence).read_text(encoding="utf-8"))
    checks=json.loads((ROOT/"validation/integration/code_checks.json").read_text(encoding="utf-8"))
    if result["status"]!="verified" or result["real_photos_verified"]!=35 or not checks["success"]:
        raise ValueError("F独立验证未完成")
    if any(sha(p)!=value for p,value in result["files"].items()): raise ValueError("验证后源码变化")
    manifest=ROOT/"validation/frozen_modules.json"
    data=json.loads(manifest.read_text(encoding="utf-8"))
    allowed={"src/soutui_image/stage_e_codex_review.py","src/soutui_image/stage_f_outputs.py"}
    for module in data["modules"].values():
        for p,value in module["files"].items():
            if p not in allowed and sha(p)!=value: raise ValueError(f"其他冻结文件变化：{p}")
    history=ROOT/"validation/history/frozen_before_F_quality5_20261029.json"
    if history.exists(): raise FileExistsError("拒绝重复覆盖F历史")
    history.write_bytes(manifest.read_bytes())
    module=data["modules"]["F"]
    previous=dict(module["files"])
    files=set(previous)|set(result["files"])|{"src/soutui_image/review_record_validation.py","tools/validate_output_withdrawal.py","tools/register_output_revision.py"}
    module["files"]={p:sha(p) for p in sorted(files)}
    module.setdefault("revisions",[]).append({"date":"2026-10-29","reason":"质量规则升级：禁止旧通过继续累计；旧输出可恢复撤回本机待复核区",
        "authorization":"用户要求修正漏判并处理完成，包含必要的旧保存状态修正","previous_files":previous,"prior_baseline":str(history.relative_to(ROOT)),"evidence":evidence})
    module["evidence"].append(evidence)
    module["revision_scope"]={"real_image_copies":35,"user_original_outputs_moved":False,"whole_reaudit_completed":False,
        "scope":"真实图片副本的九组撤回、原字节保留、重复执行及中断恢复；普通保存禁止消费旧通过"}
    temporary=manifest.with_suffix(".pending.json")
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); temporary.replace(manifest)
    record={"module":"F","status":"verified_scope","evidence":evidence,"files":module["files"],"scope":module["revision_scope"]}
    (ROOT/"validation/quality_v2/F_acceptance.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    table="\n".join(f"| `{p}` | `{value}` |" for p,value in module["files"].items())
    with (ROOT/"docs/模块验收记录.md").open("a",encoding="utf-8") as handle:
        handle.write(f"\n\n## F 旧输出升级独立验证（2026-10-29）\n\n- 真实35张旧图的隔离副本已验证：九组结构保留，旧图逐字节转存待复核区，不计新合格；原素材与原任务未改变。用户真实输出尚未移动，不把副本验证说成整批视觉重审完成。\n- 中断前后清单由日志绑定，覆盖移动后未写清单、清单已写但日志未完成的恢复；未登记素材保留，原图不改。普通保存拒绝旧通过，旧拒绝保留原十项证据。\n- 90项检查通过，真实文件证据 `{evidence}`。旧F基线保存在 `{history.relative_to(ROOT)}`。G/H尚需联调；E模型选择及35张视觉复核另计。\n\n| 文件 | 实际 SHA-256 |\n| --- | --- |\n{table}\n")
    print(json.dumps({k:v for k,v in record.items() if k!="files"},ensure_ascii=True))


if __name__=="__main__": main()
