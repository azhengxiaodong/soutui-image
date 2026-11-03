"""F 独立入口：读取当前商品已完成审核的轮次，将合格图保存到原目录九个组。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import load_plan
from soutui_image.stage_f_outputs import save_approved_reports


def main():
    parser = argparse.ArgumentParser(description="F：仅保存已双次审核通过的图片，保留全部合格图")
    parser.add_argument("directory")
    parser.add_argument("product_id")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data" / "tasks")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "review_cache")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.report and args.report.exists():
            raise FileExistsError("报告已存在，不覆盖")
        batch = scan_inputs(args.directory)
        if not batch.ready:
            raise ValueError("原图输入有异常")
        products = [p for p in batch.products if p.product_id == args.product_id]
        if len(products) != 1:
            raise ValueError("没有当前商品唯一原图")
        product = products[0]
        plan = load_plan(args.state_dir / f"product_{product.product_id}_references.json")
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(
            (args.cache_dir / product.product_id).glob("group_*/round_*/round_review.json"))]
        complete = [r for r in reports if r.get("all_successful_images_reviewed")]
        result = save_approved_reports(product, plan, complete, args.cache_dir, args.state_dir)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
