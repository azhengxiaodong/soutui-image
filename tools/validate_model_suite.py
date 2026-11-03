"""指定模型的当前完整接口实测：同两张错误图、认可局部图，全部使用实际白底原文件。"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.review_images import inspect_image
from soutui_image.stage_e_codex_review import POLICY_VERSION, review_candidate
from validate_quality_v2 import samples


def main():
    """用生产review_candidate入口实测，不复用其他模型的定位或判断，也不将标签放入工作目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=True)
    config = {"policy_version":POLICY_VERSION,"model":args.model}
    path = root / "config.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != config:
        raise ValueError("测试目录已经属于其他模型或规则")
    path.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
    cases = samples()
    canonical = cases[0]["product"]
    cases[2]["product"] = canonical
    cases[2]["candidate"] = inspect_image(canonical.product_id, "calibration-sample-03-canonical-source",
                                          ROOT / "data/evaluation/user_samples/sample_3/result.png")
    def worker(index, sample):
        try:
            report = review_candidate(sample["product"], sample["candidate"], root / "reviews", model=args.model)
            correct = report.decision == sample["expected"]
            if index == 3:
                correct = correct and all(next(c for c in p["checks"] if c["feature"] == "裤长与脚踝位置")["visibility"] == "not_visible"
                                          for p in report.passes)
            return {"case":index,"actual":report.decision,"expected":sample["expected"],"passed":correct,"review":asdict(report)}
        except Exception as error:
            return {"case":index,"actual":"error","passed":False,"error":str(error)}
    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in as_completed([executor.submit(worker,i,sample) for i,sample in enumerate(cases,1)]):
            result=future.result(); results.append(result)
            (root/"progress.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({k:v for k,v in result.items() if k!='review'},ensure_ascii=True),flush=True)
    result={**config,"status":"verified_scope" if all(r["passed"] for r in results) else "needs_review","cases":results,
            "limits":"同三张用户规则校准图，不是独立准确率测试；只验收整图结果，不宣称每一处细微变化均被定位。"}
    (root/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},ensure_ascii=True),flush=True)
    return 0 if result["status"]=='verified_scope' else 2


if __name__ == "__main__":
    raise SystemExit(main())
