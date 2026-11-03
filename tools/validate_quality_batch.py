"""只读核验35张旧通过的真实规则升级、原任务、九组保存和旧非通过证据。"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from soutui_image.pipeline import PipelinePaths
from soutui_image.quality_upgrade import needs_upgrade
from soutui_image.review_record_validation import validate_bound_review
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import load_plan
from soutui_image.stage_d_web_generation import group_decision
from soutui_image.stage_e_codex_review import POLICY_VERSION
from soutui_image.stage_f_outputs import MANIFEST_NAME, _qualified_items


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    """逐张对应历史审核与生成任务，不按新版数量反向否定旧规则下的补生决策。"""
    parser=argparse.ArgumentParser(); parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--data",type=Path,default=ROOT/"data"); parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); paths=PipelinePaths(args.data.resolve())
    batch=scan_inputs(args.input); assert batch.ready
    results=[]; totals=Counter(); old_nonpass=0
    for product in batch.products:
        pid=product.product_id
        key=hashlib.sha256((POLICY_VERSION+pid+product.sha256+"gpt-6.1-sol").encode()).hexdigest()[:24]
        operation=paths.root/"quality_revisions"/key
        applied=read(operation/"apply_result.json"); staged=read(operation/"result.json"); plan=read(operation/"plan.json")
        assert applied["status"]=="rechecked" and staged["status"]=="staged"
        assert staged["policy_version"]==POLICY_VERSION and staged["model"]=="gpt-6.1-sol"
        assert plan["source_sha256"][pid]==sha(product.source_path)==product.sha256
        expected_jobs={str(Path(x["job_path"]).resolve()):x["job_sha256"] for x in plan["inputs"] if not x.get("output_manifest")}
        actual_jobs={str(p.resolve()):sha(p) for p in (paths.tasks/pid).glob("group_*/round_*.json")}
        assert expected_jobs==actual_jobs, "原轮次已改变或有额外提交"
        reports=[]; current={}; decisions=Counter()
        for item in staged["reports"]:
            new_path=Path(item["old_path"]); old_path=operation/"history"/new_path.relative_to(paths.cache)
            assert sha(old_path)==item["old_sha256"] and sha(new_path)==item["sha256"]
            old=read(old_path); new=read(new_path); reports.append(new)
            previous={o["candidate"]["result_id"]:o for o in old["outcomes"]}
            job=read(paths.tasks/pid/f"group_{new['row']:02}"/f"round_{new['round_index']:02}.json")
            assert new["reference_ids"]==job["reference_ids"] and new["task_id"]==job["task_id"]
            assert {o["candidate"]["result_id"] for o in new["outcomes"]}=={r["result_id"] for r in job["results"] if r["status"]=="done"}
            for outcome in new["outcomes"]:
                c=outcome["candidate"]; old_item=previous[c["result_id"]]
                assert c==old_item["candidate"] and sha(c["local_path"])==c["sha256"]
                validate_bound_review(product,c,outcome["review"])
                if old_item["review"]["decision"]!="pass":
                    assert outcome==old_item, "旧非通过证据不能补造或篡改"
                    old_nonpass+=1
                else:
                    review=outcome["review"]; assert review["policy_version"]==POLICY_VERSION
                    cache_key=hashlib.sha256("\0".join((POLICY_VERSION,pid,c["result_id"],product.sha256,c["sha256"],"gpt-6.1-sol")).encode()).hexdigest()
                    assert read(paths.reviews/cache_key/"review.json")==review
                    current[(new["row"],c["sha256"])]=review
                    decisions[review["decision"]]+=1
        reference_plan=load_plan(paths.tasks/f"product_{pid}_references.json")
        _qualified_items(product,reference_plan,reports,paths.cache,paths.tasks)
        manifest=read(Path(product.source_path).parent/pid/MANIFEST_NAME)
        assert len(manifest["groups"])==9 and len(manifest["withdrawn"])==len(current)==staged["total"]
        output=Path(product.source_path).parent/pid
        saved=set()
        for group in manifest["groups"]:
            actual={p.name for p in (output/group["directory"]).iterdir() if p.is_file()}
            assert actual=={s["filename"] for s in group["saved"]}
            for entry in group["saved"]:
                identity=(group["row"],entry["sha256"]); assert identity not in saved
                saved.add(identity)
                assert entry["policy_version"]==POLICY_VERSION and current[identity]["decision"]=="pass"
                assert sha(output/group["directory"]/entry["filename"])==entry["sha256"]
        assert saved=={identity for identity,review in current.items() if review["decision"]=="pass"}
        for entry in manifest["withdrawn"]:
            identity=(entry["row"],entry["sha256"])
            assert sha(entry["retained_path"])==entry["sha256"] and entry["review_status"]!="pending"
            assert identity in current
        assert not needs_upgrade(product,paths)
        groups=[]
        for assignment in reference_plan.assignments:
            rounds=[read(p)["round_index"] for p in (paths.tasks/pid/f"group_{assignment.row:02}").glob("round_*.json")]
            choice=group_decision(paths.tasks,product,assignment)
            if rounds:
                assert max(rounds)<=3
                if choice.approved_count<3:
                    assert choice.action==("paused" if max(rounds)==3 else "generate")
                    assert choice.next_round==(None if max(rounds)==3 else max(rounds)+1)
            groups.append({"row":assignment.row,"approved":choice.approved_count,"action":choice.action,
                           "next_round":choice.next_round,"existing_rounds":sorted(rounds)})
        totals.update(decisions)
        results.append({"product_id":pid,"decisions":dict(decisions),"saved":len(saved),"retained":len(current),
                        "original_job_files":len(actual_jobs),"groups":groups})
    assert sum(totals.values())==35 and old_nonpass==54
    report={"status":"verified","policy_version":POLICY_VERSION,"model":"gpt-6.1-sol","old_passes_rechecked":35,
            "current_decisions":dict(totals),"legacy_nonpass_kept_unchanged":old_nonpass,"products":results,
            "original_images_and_round_files_unchanged":True,"new_generation_submissions":0,"rounds_reset":False,
            "scope":"35张旧通过使用当前真实审核（含已完成QA缓存）；54张旧非通过保留原十项证据，不声称89张均重审"}
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True))


if __name__=="__main__": main()
