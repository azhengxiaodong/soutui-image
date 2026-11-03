"""用上轮实际中断图验证阶段续跑，不重做已经完成的两次整图审核。"""

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import asdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from soutui_image.review_images import ReviewImage
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_e_codex_review import POLICY_VERSION,review_candidate
from soutui_image.runtime_lock import pipeline_lock


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    """实际续做两个额度中断的专项，并核对原整图响应、原图和任务字节不变。"""
    parser=argparse.ArgumentParser()
    parser.add_argument("--data",type=Path,default=ROOT/"data")
    parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    target=args.output.resolve(); target.mkdir(parents=True,exist_ok=False)
    products={p.product_id:p for p in scan_inputs(args.input).products}
    pending=[]
    for p in args.data.glob("quality_revisions/*/result.json"):
        data=json.loads(p.read_text(encoding="utf-8"))
        if data.get("status")=="incomplete": pending.extend(e for e in data["errors"] if e["status"]=="error")
    if len(pending)!=2: raise ValueError("当前不是已确认的两张中断图，需先核对断点")
    jobs={str(p):sha(p) for p in args.data.glob("tasks/*/group_*/round_*.json")}
    def worker(item):
        product=products[item["product_id"]]; candidate=ReviewImage(**item["candidate"])
        key=hashlib.sha256("\0".join((POLICY_VERSION,product.product_id,candidate.result_id,product.sha256,candidate.sha256,"gpt-6.1-sol")).encode()).hexdigest()
        folder=args.data/"reviews"/key
        original={str(p):sha(p) for p in folder.glob("*/**/*.json") if p.name=="response.json" and p.parent.name in ("first","verification")}
        original.update({str(p):sha(p) for p in folder.glob("*/**/stderr.txt") if p.parent.name in ("first","verification")})
        attempts_before={str(p) for p in folder.glob("*/**/body_attempt_*")}
        result=review_candidate(product,candidate,args.data/"reviews",model="gpt-6.1-sol")
        if not all(sha(p)==value for p,value in original.items()): raise ValueError("旧整图响应被改写")
        if attempts_before!={str(p) for p in folder.glob("*/**/body_attempt_*")}: raise ValueError("已经完成的整图阶段被重复调用")
        return {"product_id":product.product_id,"result_id":candidate.result_id,"decision":result.decision,
                "body_responses_unchanged":True,"body_model_calls_repeated":False,"review":asdict(result)}
    results=[]
    with pipeline_lock(args.data):
        with ThreadPoolExecutor(max_workers=2) as executor:
            for future in as_completed([executor.submit(worker,item) for item in pending]):
                result=future.result(); results.append(result)
                (target/"progress.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
                print(json.dumps({k:v for k,v in result.items() if k!="review"},ensure_ascii=True),flush=True)
    assert all(sha(p)==value for p,value in jobs.items())
    files=["src/soutui_image/stage_e_codex_review.py","src/soutui_image/review_checkpoints.py"]
    record={"status":"verified","policy_version":POLICY_VERSION,"model":"gpt-6.1-sol","results":results,
            "generation_jobs_unchanged":True,"new_generation_submissions":0,"files":{p:sha(ROOT/p) for p in files}}
    (target/"result.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__=="__main__": main()
