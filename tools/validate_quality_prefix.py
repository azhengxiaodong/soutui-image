"""G 独立回归：首组两轮真实历史前缀，复用五张当前真实审核，禁止任何新模型或网站调用。"""

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from soutui_image.pipeline import Pipeline,PipelinePaths
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import load_plan
from soutui_image.stage_d_web_generation import group_decision
from soutui_image.stage_e_codex_review import POLICY_VERSION
from soutui_image.stage_f_outputs import MANIFEST_NAME


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    """只建立明确标记的历史前缀副本，不伪造任务/模型结果，不改用户实际目录。"""
    parser=argparse.ArgumentParser(); parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--prepare-only",action="store_true",help="只准备真实历史副本，供桌面及exe独立回归")
    args=parser.parse_args(); root=args.output.resolve()
    original=ROOT/"data"; pid="1083284353580"
    actual_product=next(p for p in scan_inputs(r"C:\Users\王\Desktop\家居服白底").products if p.product_id==pid)
    old_manifests=[p for p in (original/"quality_withdrawn").glob("*/manifest_before.json") if read(p).get("product_id")==pid]
    if len(old_manifests)!=1: raise ValueError("没有唯一真实旧保存清单")
    manifest=read(old_manifests[0]); original_manifest=read(Path(actual_product.source_path).parent/pid/MANIFEST_NAME)
    # 五张旧通过来自实际首组两轮，先确认它们已全部有当前生产审核接口的真结果。
    source_reports=[]; expected_cache=[]
    for index in (0,1):
        relative=Path(pid)/"group_01"/f"round_{index:02}"/"round_review.json"
        path=original/"review_cache"/relative
        if read(path).get("quality_revision",{}).get("policy_version")==POLICY_VERSION:
            candidates=list((original/"quality_revisions").glob("*/history/"+str(relative)))
            if len(candidates)!=1: raise ValueError("缺少唯一旧审核历史")
            path=candidates[0]
        report=read(path)
        for item in report["outcomes"]:
            if item["review"]["decision"]!="pass": continue
            c=item["candidate"]
            key=hashlib.sha256("\0".join((POLICY_VERSION,pid,c["result_id"],actual_product.sha256,c["sha256"],"gpt-6.1-sol")).encode()).hexdigest()
            cached=original/"reviews"/key/"review.json"
            if not cached.exists(): raise ValueError("首组新版真实审核尚未全部完成")
            expected_cache.append((key,cached))
        source_reports.append((relative,path,report))
    if len(expected_cache)!=5: raise ValueError("历史前缀不是五张旧通过")
    root.mkdir(parents=True,exist_ok=False)
    inputs=root/"input"; inputs.mkdir()
    shutil.copyfile(actual_product.source_path,inputs/Path(actual_product.source_path).name)
    data=root/"data"; tasks=data/"tasks"/pid/"group_01"; tasks.mkdir(parents=True)
    shutil.copyfile(original/"tasks"/f"product_{pid}_references.json",data/"tasks"/f"product_{pid}_references.json")
    fixture_links=[]
    for relative,path,report in source_reports:
        destination=data/"review_cache"/relative; destination.parent.mkdir(parents=True,exist_ok=True)
        adapted=copy.deepcopy(report)
        for item in adapted["outcomes"]:
            source=Path(item["candidate"]["local_path"]).resolve()
            target=data/"review_cache"/source.relative_to((original/"review_cache").resolve())
            target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(source,target)
            if sha(target)!=item["candidate"]["sha256"]: raise ValueError("图片副本字节不符")
            item["candidate"]["local_path"]=str(target)
        destination.write_text(json.dumps(adapted,ensure_ascii=False,indent=2),encoding="utf-8")
        fixture_links.append({"actual_report":str(path),"actual_sha256":sha(path),"fixture_report":str(destination),
                              "adaptation":"仅将candidate.local_path指向同字节副本，其他审核结论与身份不变"})
        job=original/"tasks"/pid/"group_01"/f"round_{report['round_index']:02}.json"
        shutil.copyfile(job,tasks/job.name)
    for key,path in expected_cache:
        target=data/"reviews"/key/"review.json"; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(path,target)
    summary=original/"tasks"/pid/"group_01"/"review_summary.json"
    if read(summary).get("policy_version")==POLICY_VERSION:
        options=[p for p in summary.parent.glob("review_history/*.json") if read(p).get("policy_version")!=POLICY_VERSION]
        if len(options)!=1: raise ValueError("缺少首组唯一旧汇总")
        summary=options[0]
    shutil.copyfile(summary,tasks/"review_summary.json")
    output=inputs/pid; output.mkdir()
    retained={(x["row"],x["sha256"]):Path(x["retained_path"]) for x in original_manifest["withdrawn"]}
    for group in manifest["groups"]:
        (output/group["directory"]).mkdir()
        if group["row"]!=1: group["saved"]=[]
        for item in group["saved"]:
            source=retained[(group["row"],item["sha256"])]
            shutil.copyfile(source,output/group["directory"]/item["filename"])
    (output/MANIFEST_NAME).write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    (root/"fixture_links.json").write_text(json.dumps(fixture_links,ensure_ascii=False,indent=2),encoding="utf-8")
    job_hashes={str(p):sha(p) for p in tasks.glob("round_*.json")}
    if args.prepare_only:
        (root/"prepared.json").write_text(json.dumps({"product_id":pid,"old_saved":5,"cached_reviews":5,
            "task_sha256":job_hashes,"source_sha256":actual_product.sha256},ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"status":"prepared","root":str(root)},ensure_ascii=True)); return
    def forbidden(*a,**k): raise AssertionError("本回归不得调用网站或重新调用模型")
    with patch("soutui_image.stage_e_codex_review._audit_once",side_effect=forbidden),patch("soutui_image.pipeline.WebTransport",side_effect=forbidden):
        runner=Pipeline(PipelinePaths(data),model="gpt-6.1-sol")
        first=runner.recheck_saved(inputs); second=runner.recheck_saved(inputs)
    assert first["status"]==second["status"]=="complete"
    assert second["products"][0]["status"]=="already_current"
    assert all(sha(p)==value for p,value in job_hashes.items())
    result_manifest=read(output/MANIFEST_NAME)
    for g in result_manifest["groups"]:
        for item in g["saved"]:
            assert item["policy_version"]==POLICY_VERSION and sha(output/g["directory"]/item["filename"])==item["sha256"]
    for item in result_manifest["withdrawn"]: assert sha(item["retained_path"])==item["sha256"]
    product=scan_inputs(inputs).products[0]; plan=load_plan(data/"tasks"/f"product_{pid}_references.json")
    choice=group_decision(data/"tasks",product,plan.assignments[0])
    result={"status":"verified","scope":"实际首组两轮10张成功结果、其中5张旧通过的历史前缀副本，当前审核为真实缓存；不声称整35张已完成",
            "first":first,"second":second,"job_files_unchanged":True,"new_model_calls":0,"new_generation_submissions":0,
            "current_saved":sum(len(g["saved"]) for g in result_manifest["groups"]),"next_group_action":choice.action,"next_round":choice.next_round}
    (root/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("first","second")},ensure_ascii=True))


if __name__=="__main__": main()
