"""板块 B 独立入口：读取一张指定商品，调用 Codex 分类，在现有浏览器创建项目。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.codex_clothing import classify_clothing
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_b_projects import run_stage_b, verify_current


def main():
    parser = argparse.ArgumentParser(description="板块 B：复用现有浏览器，分类并上传一张白底图")
    parser.add_argument("directory", help="白底图文件夹")
    parser.add_argument("product_id", help="本次验证的商品 ID")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "validation" / "stage_b" / "runs")
    parser.add_argument("--codex-exe", help="目标电脑的 Codex 程序路径，可省略自动查找")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--classify-only", action="store_true", help="只验证分类，不操作网站")
    mode.add_argument("--verify-current", action="store_true", help="核对成功记录与当前项目，不重复创建")
    args = parser.parse_args()
    try:
        batch = scan_inputs(args.directory)
        if not batch.ready:
            raise ValueError(f"输入目录有异常：{batch.to_dict()}")
        products = [p for p in batch.products if p.product_id == args.product_id]
        if len(products) != 1:
            raise ValueError("指定商品 ID 不在有效原图列表中")
        if args.classify_only:
            result = classify_clothing(products[0], args.state_dir, codex_exe=args.codex_exe)
        elif args.verify_current:
            result = verify_current(products[0], args.state_dir)
        else:
            result = run_stage_b(products[0], args.state_dir, codex_exe=args.codex_exe)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, OSError, TimeoutError) as error:
        print(json.dumps({"status": "needs_attention", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
