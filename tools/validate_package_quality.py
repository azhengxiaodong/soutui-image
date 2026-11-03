"""验收本次 exe 的规则、自检、真实历史副本续跑及桌面窗口，不新增模型或生成。"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path): return json.loads(path.read_text(encoding="utf-8"))
def snapshot(paths): return {str(p): sha(p) for p in paths}


def main():
    """输出本次可执行文件哈希绑定的真实证据；不引用旧exe的成功报告。"""
    parser=argparse.ArgumentParser(); parser.add_argument("--exe",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True); args=parser.parse_args()
    exe=args.exe.resolve(); target=args.output.resolve(); target.mkdir(parents=True,exist_ok=False)
    def run(*arguments):
        result=subprocess.run([str(exe),*map(str,arguments)],timeout=120,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if result.returncode: raise RuntimeError(f"exe入口失败：{arguments}，退出{result.returncode}")
    run("--self-test","--data-dir",target/"self_test")
    smoke=read(target/"self_test/self_test_result.json")
    assert smoke["status"]=="ok" and smoke["policy_version"]=="2026-10-29.5"
    assert smoke["review_model"]=="gpt-6.1-sol" and smoke["audit_timeout_per_call_seconds"]==600
    fixture=target/"prefix"
    subprocess.run([sys.executable,str(ROOT/"tools/validate_quality_prefix.py"),"--output",str(fixture),"--prepare-only"],check=True)
    data=fixture/"data"; inputs=fixture/"input"; pid=read(fixture/"prepared.json")["product_id"]
    jobs=snapshot(data.glob("tasks/*/group_*/round_*.json"))
    reviews=snapshot(p for p in (data/"reviews").rglob("*") if p.is_file())
    run("--input",inputs,"--data-dir",data,"--recheck-saved")
    first=read(data/"last_run_result.json"); assert first["status"]=="complete"
    run("--input",inputs,"--data-dir",data,"--recheck-saved")
    second=read(data/"last_run_result.json"); assert second["products"][0]["status"]=="already_current"
    assert jobs==snapshot(data.glob("tasks/*/group_*/round_*.json"))
    assert reviews==snapshot(p for p in (data/"reviews").rglob("*") if p.is_file()), "exe不应新调模型"
    manifest=read(inputs/pid/".搜推生图.json")
    assert len(manifest["groups"])==9 and sum(len(g["saved"]) for g in manifest["groups"])==1
    for g in manifest["groups"]:
        for item in g["saved"]:
            assert item["policy_version"]==smoke["policy_version"] and sha(inputs/pid/g["directory"]/item["filename"])==item["sha256"]
    for item in manifest["withdrawn"]: assert sha(Path(item["retained_path"]))==item["sha256"]
    events=[json.loads(line) for line in (data/"last_run.events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert not any(x["kind"]=="submitting" for x in events)
    run("--input",inputs,"--data-dir",data,"--gui","--ui-smoke-output",target/"gui")
    gui=read(target/"gui/smoke.json")
    assert gui["packaged"] and gui["window_created"] and gui["recheck_button_present"] and not gui["generation_started"]
    assert gui["current_counts"][pid]==1 and gui["group_values"][0][2]=="待补生", "exe界面仍为旧状态"
    record={"status":"verified","executable":str(exe),"executable_sha256":sha(exe),"policy_version":smoke["policy_version"],
            "review_model":smoke["review_model"],"self_test":smoke,"gui":gui,"first":first,"second":second,
            "new_model_calls":0,"new_generation_submissions":0,"job_files_unchanged":True,"current_saved":1,
            "scope":"本机exe，首组历史副本复核和九目录保存，原五张留存；非另一台电脑或完整多商品生图验收"}
    (target/"result.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in record.items() if k not in ("first","second","gui","self_test")},ensure_ascii=True))


if __name__=="__main__": main()
