"""F 独立实测：复制真实35张旧输出到隔离测试目录，再验证撤回及原素材不变。"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import load_plan
from soutui_image.stage_f_output_revision import withdraw_legacy_outputs
from soutui_image.stage_f_outputs import MANIFEST_NAME


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    """只动新的测试副本；核对真实旧输出、原图、任务记录未改变。"""
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    target=args.output.resolve(); target.mkdir(parents=True,exist_ok=False)
    inputs=target/"input"; inputs.mkdir()
    batch=scan_inputs(args.input)
    if not batch.ready: raise ValueError("原图目录扫描不通过")
    original_files={}
    plans={}
    expected={}
    for product in batch.products:
        original=Path(product.source_path)
        original_files[str(original)]=sha(original)
        shutil.copyfile(original,inputs/original.name)
        plan=load_plan(ROOT/"data/tasks"/f"product_{product.product_id}_references.json")
        plans[product.product_id]=plan
        source=original.parent/product.product_id
        manifest=source/MANIFEST_NAME
        record=json.loads(manifest.read_text(encoding="utf-8"))
        original_files[str(manifest)]=sha(manifest)
        copied=inputs/product.product_id; copied.mkdir()
        shutil.copyfile(manifest,copied/MANIFEST_NAME)
        for group in record["groups"]:
            directory=copied/group["directory"]; directory.mkdir()
            for item in group["saved"]:
                path=source/group["directory"]/item["filename"]
                if sha(path)!=item["sha256"]: raise ValueError("真实旧输出已变更")
                original_files[str(path)]=item["sha256"]
                shutil.copyfile(path,directory/item["filename"])
                expected[(product.product_id,item["sha256"])]=item
    jobs={str(p):sha(p) for p in (ROOT/"data/tasks").glob("*/group_*/round_*.json")}
    outcomes=[]
    for product in scan_inputs(inputs).products:
        result=withdraw_legacy_outputs(product,plans[product.product_id],target/"holding")
        record=json.loads((Path(product.source_path).parent/product.product_id/MANIFEST_NAME).read_text(encoding="utf-8"))
        assert all(not g["saved"] for g in record["groups"])
        for item in record["withdrawn"]:
            assert (product.product_id,item["sha256"]) in expected
            assert sha(item["retained_path"])==item["sha256"]
            assert item["review_status"]=="pending"
        again=withdraw_legacy_outputs(product,plans[product.product_id],target/"holding")
        assert again["withdrawn"]==0
        outcomes.append(result)
    assert all(sha(p)==value for p,value in original_files.items())
    assert all(sha(p)==value for p,value in jobs.items())
    files=["src/soutui_image/stage_f_outputs.py","src/soutui_image/stage_f_output_revision.py","tests/test_output_revision.py"]
    result={"status":"verified","scope":"真实35张图片的独立测试副本；未迁移用户实际输出目录",
            "real_photos_verified":len(expected),"outcomes":outcomes,"original_files_unchanged":True,"generation_jobs_unchanged":True,
            "new_generation_submitted":False,"files":{p:sha(ROOT/p) for p in files}}
    (target/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("outcomes","files")},ensure_ascii=True))


if __name__=="__main__": main()
