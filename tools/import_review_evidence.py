"""复用本轮生产审核接口对真实网站结果的实证；不导入校准局部图、不伪造模型结论。"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from soutui_image.stage_e_codex_review import POLICY_VERSION


def main():
    """只有ID/原图/结果哈希与原网站报告相同的当前版本实证才进入本机审核缓存。"""
    parser=argparse.ArgumentParser()
    parser.add_argument("suite",type=Path)
    parser.add_argument("--data",type=Path,default=ROOT/"data")
    args=parser.parse_args()
    suite=json.loads((args.suite/"result.json").read_text(encoding="utf-8"))
    if suite["status"]!="verified_scope" or suite["policy_version"]!=POLICY_VERSION:
        raise ValueError("同版本真实校准未通过")
    registered=set()
    for path in (args.data/"review_cache").glob("*/group_*/round_*/round_review.json"):
        for item in json.loads(path.read_text(encoding="utf-8"))["outcomes"]:
            r=item["review"]
            registered.add((r["product_id"],r["result_id"],r["source_sha256"],r["result_sha256"]))
    count=0
    for case in suite["cases"]:
        report=case["review"]
        identity=tuple(report[k] for k in ("product_id","result_id","source_sha256","result_sha256"))
        if identity not in registered: continue
        key=hashlib.sha256("\0".join((POLICY_VERSION,*identity,suite["model"])).encode()).hexdigest()
        source=Path(report["evidence_directory"]).parent/"review.json"
        if source.parent.name!=key or json.loads(source.read_text(encoding="utf-8"))!=report:
            raise ValueError("实证缓存与模型/图片身份不符")
        destination=args.data/"reviews"/key/"review.json"; destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():
            if json.loads(destination.read_text(encoding="utf-8"))!=report:
                raise ValueError("已有另一次真实审核，未覆盖")
        else: shutil.copyfile(source,destination)
        count+=1
    record={"model":suite["model"],"policy_version":POLICY_VERSION,"real_result_reviews_reused":count,
            "manual_sample_imported":False,"raw_model_evidence_preserved":True}
    (args.suite/"operational_cache_import.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(record,ensure_ascii=True))


if __name__=="__main__": main()
