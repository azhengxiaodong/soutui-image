"""E 独立入口：一组内每张成功结果分别审核；缓存不是合格输出目录。"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.review_images import download_result
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_e_codex_review import review_candidate
from soutui_image.review_group_state import write_group_review_summary


def main():
    parser = argparse.ArgumentParser(description="E：逐张核对当前商品与生成结果，拟通过者独立复核")
    parser.add_argument("directory")
    parser.add_argument("product_id")
    parser.add_argument("--row", type=int, choices=range(1, 10), required=True)
    parser.add_argument("--round", type=int, choices=range(4), default=0, dest="round_index")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data" / "tasks")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "review_cache")
    parser.add_argument("--review-dir", type=Path, default=ROOT / "data" / "reviews")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--model", help="可选：当前 Codex 账号已支持的模型；省略使用账号默认模型")
    parser.add_argument("--download-only", action="store_true", help="仅获取审核临时素材，不调用模型、不当作合格图")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.report and args.report.exists():
            raise FileExistsError("报告已存在，不覆盖旧证据")
        batch = scan_inputs(args.directory)
        if not batch.ready:
            raise ValueError("原图输入有异常")
        products = [p for p in batch.products if p.product_id == args.product_id]
        if len(products) != 1:
            raise ValueError("没有当前商品对应的唯一原图")
        product = products[0]
        job_path = args.state_dir / product.product_id / f"group_{args.row:02}" / f"round_{args.round_index:02}.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        if (job["product_id"] != product.product_id or job["source_sha256"] != product.sha256
                or job["row"] != args.row or job["round_index"] != args.round_index):
            raise ValueError("任务记录不属于当前商品与组号")
        if job["state"] not in ("awaiting_audit", "generation_failed"):
            raise ValueError("当前生成任务尚未完成，不启动整轮审核")
        results = [r for r in job.get("results", []) if r["status"] == "done"]
        if len(results) != job["successful_results"] or len({r["result_id"] for r in results}) != len(results):
            raise ValueError("本轮成功结果清单与数量或身份不符")
        root = args.cache_dir / product.product_id / f"group_{args.row:02}" / f"round_{args.round_index:02}"
        items = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(download_result, product.product_id, r, root): r for r in results}
            for future in as_completed(futures):
                result = futures[future]
                candidate = future.result()
                items.append({"reference_id": result["reference_id"], "reference_index": result["reference_index"],
                              "candidate": asdict(candidate), "_object": candidate})
        items.sort(key=lambda item: item["reference_index"])
        manifest = {"product_id": product.product_id, "source_sha256": product.sha256, "row": args.row,
                    "round_index": args.round_index, "task_id": job["task_id"], "folder_id": job["folder_id"],
                    "reference_ids": job["reference_ids"], "failed_generation_count": job["failed_results"],
                    "items": [{k: v for k, v in item.items() if k != "_object"} for item in items]}
        root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.download_only:
            print(json.dumps({"status": "cached_for_review", "items": manifest["items"],
                              "approved_output_written": False}, ensure_ascii=True, indent=2))
            return 0
        outcomes = []
        def worker(item):
            try:
                review = review_candidate(product, item["_object"], args.review_dir, model=args.model)
                return {"candidate": asdict(item["_object"]), "reference_id": item["reference_id"],
                        "reference_index": item["reference_index"], "review": asdict(review)}
            except Exception as error:
                return {"candidate": asdict(item["_object"]), "reference_id": item["reference_id"],
                        "reference_index": item["reference_index"], "error": str(error)}
        progress_path = root / "review_progress.json"
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(worker, item) for item in items]
            for future in as_completed(futures):
                outcome = future.result(); outcomes.append(outcome)
                print(json.dumps({"result_id": outcome["candidate"]["result_id"],
                                  "decision": outcome.get("review", {}).get("decision", "error"),
                                  "completed": len(outcomes), "total": len(items)}, ensure_ascii=True), flush=True)
                progress_path.write_text(json.dumps({**manifest, "outcomes": outcomes}, ensure_ascii=False, indent=2), encoding="utf-8")
        complete = all("review" in item for item in outcomes)
        final = {**manifest, "status": "reviewed" if complete else "review_incomplete",
                 "all_successful_images_reviewed": complete, "outcomes": sorted(outcomes, key=lambda item: item["reference_index"]),
                 "pass_count": sum(item.get("review", {}).get("decision") == "pass" for item in outcomes),
                 "reject_count": sum(item.get("review", {}).get("decision") == "reject" for item in outcomes),
                 "review_count": sum(item.get("review", {}).get("decision") == "review" for item in outcomes),
                 "approved_output_written": False}
        canonical = root / "round_review.json"
        temporary = canonical.with_suffix(".tmp")
        temporary.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(canonical)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as handle:
                json.dump(final, handle, ensure_ascii=False, indent=2)
        if complete:
            write_group_review_summary(product, args.state_dir, args.cache_dir, args.row)
        print(json.dumps({key: final[key] for key in ("status", "pass_count", "reject_count", "review_count", "all_successful_images_reviewed")}, ensure_ascii=True), flush=True)
        return 0 if complete else 2
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
