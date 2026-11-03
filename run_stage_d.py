"""板块 D 独立入口：默认仅准备；--submit 才真实提交当前允许的一组五张。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_b_web_upload import WebUploadReceipt
from soutui_image.stage_c_web_plan import load_plan, parse_favorites, validate_plan
from soutui_image.stage_d_web_generation import group_decision, prepare_job, query_job, submit_job, watch_job
from soutui_image.web_session import MusefitReadClient
from soutui_image.web_transport import WebTransport


def main():
    parser = argparse.ArgumentParser(description="D：固定参数的五张生成与最多三轮补生")
    parser.add_argument("directory")
    parser.add_argument("product_id")
    parser.add_argument("--row", type=int, choices=range(1, 10), required=True)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data" / "tasks")
    parser.add_argument("--session", type=Path, default=ROOT / "data" / "local" / "musefit_session.dpapi")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit", action="store_true")
    mode.add_argument("--query", action="store_true")
    parser.add_argument("--wait", action="store_true", help="提交或查询后只读跟踪至完成，有时间上限")
    parser.add_argument("--wait-timeout", type=int, default=2100)
    args = parser.parse_args()
    try:
        batch = scan_inputs(args.directory)
        if not batch.ready:
            raise ValueError("商品原图输入异常")
        products = [p for p in batch.products if p.product_id == args.product_id]
        if len(products) != 1:
            raise ValueError("商品 ID 没有唯一原图")
        product = products[0]
        plan = load_plan(args.state_dir / f"product_{product.product_id}_references.json")
        group = next(g for g in plan.assignments if g.row == args.row)
        catalog = parse_favorites(MusefitReadClient(args.session).get_json("/api/web/favorites", {"mediaType": "image"}))
        validate_plan(plan, product, catalog)
        upload = WebUploadReceipt(**json.loads((args.state_dir / f"product_{product.product_id}_upload.json").read_text(encoding="utf-8"))["receipt"])
        client = WebTransport(args.session)
        decision = group_decision(args.state_dir, product, group)
        if args.query:
            paths = sorted((args.state_dir / product.product_id / f"group_{args.row:02}").glob("round_*.json"))
            if not paths:
                raise ValueError("当前组尚无任务记录")
            path = paths[-1]
            record = query_job(path, client)
        else:
            path, record = prepare_job(product, upload, plan, args.row, args.state_dir, client)
            if args.submit:
                record = submit_job(path, client)
        if args.wait:
            if record["state"] == "prepared":
                raise ValueError("任务尚未提交，不能等待；准备参数不会自动生成")
            def progress(item):
                print(json.dumps({"state": item["state"], "successful": item.get("successful_results", 0),
                                  "failed": item.get("failed_results", 0)}, ensure_ascii=True), flush=True)
            record = watch_job(path, client, timeout=args.wait_timeout, on_progress=progress)
        print(json.dumps({"product_id": product.product_id, "row": args.row, "round_index": record["round_index"],
                          "state": record["state"], "task_id": record.get("task_id"),
                          "quoted_credits": record["quoted_credits"], "expected_results": record["expected_results"],
                          "successful_results": record.get("successful_results", 0),
                          "failed_results": record.get("failed_results", 0), "job_file": str(path),
                          "decision_before_action": decision.action}, ensure_ascii=True, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, StopIteration) as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
