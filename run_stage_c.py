"""板块 C 独立入口：直接读取网站收藏，固定九组共四十五张参考图；不提交生成。"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import prepare_references
from soutui_image.web_session import MusefitReadClient


def main():
    parser = argparse.ArgumentParser(description="板块 C：通过接口固定九个不同收藏组，不生成图片")
    parser.add_argument("directory", help="白底图片目录")
    parser.add_argument("product_id", help="当前商品 ID")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data" / "tasks")
    parser.add_argument("--session", type=Path, default=ROOT / "data" / "local" / "musefit_session.dpapi")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.report and args.report.exists():
            raise FileExistsError("报告已存在，请指定新的报告文件；不会覆盖证据")
        batch = scan_inputs(args.directory)
        if not batch.ready:
            raise ValueError("白底图片输入有异常，请先运行板块 A 检查")
        selected = [p for p in batch.products if p.product_id == args.product_id]
        if len(selected) != 1:
            raise ValueError("没有当前商品 ID 对应的唯一原图")
        start = time.perf_counter()
        data = MusefitReadClient(args.session).get_json("/api/web/favorites", {"mediaType": "image"})
        fetch_seconds = round(time.perf_counter() - start, 3)
        plan = prepare_references(selected[0], data, args.state_dir)
        report = {"status": "references_ready", "product_id": plan.product_id,
                  "source_sha256": plan.source_sha256, "fetch_seconds": fetch_seconds,
                  "candidate_groups": plan.candidate_group_count, "selected_groups": len(plan.assignments),
                  "total_references": sum(len(a.references) for a in plan.assignments),
                  "assignments": [{"row": a.row, "folder_id": a.folder_id, "group_name": a.group_name,
                                   "image_ids": [r.image_id for r in a.references]} for a in plan.assignments],
                  "generation_submitted": False}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
