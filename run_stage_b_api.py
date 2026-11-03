"""板块 B 接口变体独立入口：分类、上传并验证同一原图，重跑复用成功回执。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.codex_clothing import ClothingClassification, classify_clothing
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_b_web_upload import upload_product


def main():
    parser = argparse.ArgumentParser(description="B 接口变体：Codex 分类和原图上传，不生成图片")
    parser.add_argument("directory")
    parser.add_argument("product_id")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data" / "tasks")
    parser.add_argument("--session", type=Path, default=ROOT / "data" / "local" / "musefit_session.dpapi")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.report and args.report.exists():
            raise FileExistsError("报告已存在，不覆盖")
        batch = scan_inputs(args.directory)
        if not batch.ready:
            raise ValueError("原图输入存在异常")
        selected = [p for p in batch.products if p.product_id == args.product_id]
        if len(selected) != 1:
            raise ValueError("商品 ID 无对应唯一原图")
        product = selected[0]
        previous = args.state_dir / f"product_{product.product_id}_upload.json"
        if previous.exists():
            record = json.loads(previous.read_text(encoding="utf-8"))
            data = record["classification"]
            data["visible_garments"] = tuple(data["visible_garments"])
            classification = ClothingClassification(**data)
        else:
            classification = classify_clothing(product, args.state_dir / "classifications")
        receipt = upload_product(product, classification, args.state_dir, args.session)
        report = {"status": "uploaded_and_verified", **asdict(receipt), "generation_submitted": False}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
