"""登记D/E/F/G规则上下文回归范围，H仍保留旧基线等待桌面实测。"""

import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def read(p): return json.loads((ROOT/p).read_text(encoding="utf-8"))
def sha(p): return hashlib.sha256((ROOT/p).read_bytes()).hexdigest()


def main():
    prefix=read("validation/updates/U_PIPELINE/prefix/result.json")
    batch=read("validation/updates/U_PIPELINE/actual_batch/result.json")
    checks=read("validation/integration/code_checks.json")
    assert prefix["status"]==batch["status"]=="verified" and checks["success"]
    assert batch["old_passes_rechecked"]==35 and batch["current_decisions"]=={"reject":22,"pass":11,"review":2}
    assert prefix["next_round"]==2 and prefix["new_model_calls"]==0
    allowed={f"src/soutui_image/{p}.py" for p in ("stage_d_web_generation","stage_f_outputs","stage_f_output_revision",
        "review_group_state","review_record_validation","review_revision","quality_upgrade","pipeline")}
    verified_e=read("validation/updates/U_E_RUNTIME/result.json")
    for p,value in verified_e["files"].items(): assert sha(p)==value, "E独立验收后发生变化"
    allowed|={"src/soutui_image/stage_e_codex_review.py","src/soutui_image/review_detail_locator.py","src/soutui_image/review_button_edges.py"}
    manifest=ROOT/"validation/frozen_modules.json"; previous=manifest.read_bytes(); data=json.loads(previous)
    for name,module in data["modules"].items():
        for p,value in module["files"].items():
            if sha(p)!=value and p not in allowed: raise ValueError("出现非授权变动："+p)
    target=ROOT/"validation/updates/U_PIPELINE"; (target/"frozen_before.json").write_bytes(previous)
    files=allowed|{"src/soutui_image/update_config.py","tools/register_private_core.py"}
    evidence=["validation/updates/U_PIPELINE/prefix/result.json","validation/updates/U_PIPELINE/actual_batch/result.json"]
    for name,module in data["modules"].items():
        if name=="H_DESKTOP": continue
        changed={p:sha(p) for p in module["files"] if p in allowed}
        if changed:
            module.setdefault("revisions",[]).append({"reason":"用户明确允许私有规则上下文接入；旧合格图和三轮补生不变","previous_files":dict(module["files"]),"evidence":evidence})
            module["files"].update(changed); module["status"]="verified"; module.pop("pending_reason",None)
            module["evidence"]=list(dict.fromkeys(module["evidence"]+evidence))
    result={"status":"verified","scope":"实际历史前缀复核、保存去重、下一补生第2轮；真实35份当前图像记录与19个原生成任务不变；没有新增模型调用",
            "evidence":evidence,"files":{p:sha(p) for p in sorted(files)},"cloud_calls":0,"original_saved_counts":[10,1]}
    data["modules"]["U_PIPELINE"]={"status":"verified","scope":result["scope"],"files":result["files"],"evidence":evidence}
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (target/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    with (ROOT/"docs/模块验收记录.md").open("a",encoding="utf-8") as handle:
        handle.write("\n\n## U_PIPELINE商品固定规则与公共依赖回归\n\n用户明确授权D/F/G/H及相关公共依赖修改。真实首组两轮前缀副本复核及保存通过，下一补生仍第2轮；真实35份完成记录与原图、19个任务不变，两商品仍保存10/1张。没有新模型调用。规则通过商品上下文传至并行线程，旧商品固定规则，新商品选择新稳定版的签名传输实测见U_GITHUB。复现`python tools/validate_quality_prefix.py --output 新目录`及`python tools/validate_quality_batch.py --input 白底图目录 --output 新目录`；证据`validation/updates/U_PIPELINE`。H保持待回归。\n\n| 文件 | 实际SHA-256 |\n| --- | --- |\n")
        for f,v in result["files"].items(): handle.write(f"| `{f}` | `{v}` |\n")
    print(json.dumps({"status":"verified","updated_modules":["D","E公共依赖","F","G_PIPELINE","U_PIPELINE"],"H":"requires_regression"},ensure_ascii=True))


if __name__=="__main__": main()
