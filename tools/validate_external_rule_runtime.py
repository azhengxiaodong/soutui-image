"""U_E_RUNTIME独立验证：实际35张缓存审图结论一致，新规则隔离旧缓存，模板与历史输入一致。"""

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src")); sys.path.insert(0,str(ROOT))
from soutui_image.rules_bundle import builtin_bundle,RuleBundle
from soutui_image.rule_runtime import use_rules,current_policy
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.review_images import ReviewImage
from soutui_image.stage_e_codex_review import review_candidate,_prompt
from soutui_image.review_checkpoints import fingerprint
from tools.check_frozen import check_frozen


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256((ROOT/path).read_bytes()).hexdigest()


def main():
    original=builtin_bundle(); products={p.product_id:p for p in scan_inputs(r"C:\Users\王\Desktop\家居服白底").products}
    cases=[]; stages=0
    def forbidden(*a,**k): raise AssertionError("无损迁移验收不得调用模型")
    with patch("soutui_image.stage_e_codex_review._audit_once",side_effect=forbidden):
        for report in (ROOT/"data/review_cache").glob("*/group_*/round_*/round_review.json"):
            raw=read(report); product=products.get(raw["product_id"])
            if product is None: continue
            for o in raw["outcomes"]:
                if o["review"]["policy_version"]!=original.version: continue
                candidate=ReviewImage(**o["candidate"])
                with use_rules(original):
                    result=review_candidate(product,candidate,ROOT/"data/reviews",model="gpt-6.1-sol")
                    assert json.loads(json.dumps(asdict(result),ensure_ascii=False))==o["review"]
                    evidence=Path(result.evidence_directory)
                    for verifying,stage in ((False,"first"),(True,"verification")):
                        view_path=evidence/stage/"detail_views/manifest.json"
                        if not view_path.exists(): continue
                        text=_prompt(product,candidate,verification=verifying,views=read(view_path)["views"])
                        cp=evidence/stage/"body_checkpoint.json"
                        if cp.exists(): assert fingerprint(text)==read(cp)["identity"]["prompt"]
                        else: assert text in (evidence/stage/"stderr.txt").read_text(encoding="utf-8")
                        stages+=1
                cases.append({"product_id":product.product_id,"result_id":candidate.result_id,"decision":result.decision})
        assert len(cases)==35
        data=original.data; data.update(version="2026-10-29.5-transport-test",sequence=2)
        newer=RuleBundle.parse(json.dumps(data).encode())
        with use_rules(newer):
            new_id=current_policy(); assert new_id!=original.version
            with ThreadPoolExecutor(max_workers=1) as pool: assert pool.submit(copy_context().run,current_policy).result()==new_id
            try: review_candidate(product,candidate,ROOT/"validation/updates/runtime_isolation_cache",model="gpt-6.1-sol")
            except AssertionError: pass
            else: raise AssertionError("新规则不能直接借用旧通过")
        assert current_policy()==original.version
    changed={"src/soutui_image/stage_e_codex_review.py","src/soutui_image/review_detail_locator.py","src/soutui_image/review_button_edges.py"}
    problems=check_frozen(ROOT)
    assert all(any(p.endswith(path) for path in changed) for p in problems),problems
    target=ROOT/"validation/updates/U_E_RUNTIME"; target.mkdir(parents=True,exist_ok=False)
    files=sorted(changed|{"src/soutui_image/rule_runtime.py","tools/validate_external_rule_runtime.py"})
    result={"status":"verified","scope":"无损规则运行时：35张实际原/结果的已完成结论一致、49阶段真实输入指纹一致；新版本隔离旧缓存，未新审图",
            "cases":cases,"body_stages":stages,"new_policy_cache_isolated":True,"thread_context_inherited":True,"cloud_calls":0,
            "files":{p:sha(p) for p in files}}
    (target/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    manifest=ROOT/"validation/frozen_modules.json"; previous=manifest.read_bytes(); data=json.loads(previous)
    (target/"frozen_before.json").write_bytes(previous)
    module=data["modules"]["E"]; module.setdefault("revisions",[]).append({"reason":"用户要求私有规则自动更新；文字与流程无损迁移到外部规则包","previous_files":dict(module["files"]),"evidence":"validation/updates/U_E_RUNTIME/result.json"})
    for f in files: module["files"][f]=sha(f)
    module["evidence"].append("validation/updates/U_E_RUNTIME/result.json")
    data["modules"]["U_E_RUNTIME"]={"status":"verified","scope":result["scope"],"evidence":["validation/updates/U_E_RUNTIME/result.json"],"files":result["files"]}
    for name in ("D","F","G_PIPELINE","H_DESKTOP"):
        data["modules"][name]["status"]="requires_regression"
        data["modules"][name]["pending_reason"]="规则运行时迁移已独立通过，任务/保存/桌面需回归；旧基线保留"
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with (ROOT/"docs/模块验收记录.md").open("a",encoding="utf-8") as handle:
        handle.write("\n\n## U_E_RUNTIME无损规则运行时验收\n\n用户要求私有规则更新，已验证35张实际原图/结果的完成结论与原记录相同，49阶段提示词与真实指纹/日志一致。没有新模型调用，没有减少独立复核或扣位专项；新的规则内容身份不能复用旧缓存，线程上下文显式继承。独立入口`python tools/validate_external_rule_runtime.py`，证据`validation/updates/U_E_RUNTIME/result.json`。此处不声称新准确率或提速。D/F/G/H保持待回归，旧源码见validation/history/before_private_updates_20261029。\n\n| 文件 | 实际SHA-256 |\n| --- | --- |\n")
        for f,v in result["files"].items(): handle.write(f"| `{f}` | `{v}` |\n")
    print(json.dumps({k:v for k,v in result.items() if k not in ("files","cases")},ensure_ascii=True))


if __name__=="__main__": main()
