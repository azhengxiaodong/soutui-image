"""从用户已给出的对照截图裁出原图与结果区，作为审核校准样本，不改写原截图。

裁剪区域已经逐张目视核对，去除网页按钮和缩略图；红圈保留，模型指令要求忽略标记。
校准标签只写到测试清单，不传给审核模型。
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.review_images import inspect_image
from soutui_image.stage_a_inputs import scan_inputs


def main():
    source = ROOT / "docs" / "审核样例"
    root = ROOT / "data" / "evaluation" / "user_samples"
    root.mkdir(parents=True, exist_ok=True)
    manifest = []
    for number in (1, 2, 3, 4, 6):
        matches = list(source.glob(f"图{number}-*.png"))
        if len(matches) != 1:
            raise ValueError("用户校准截图缺失或重复")
        sample = root / f"sample_{number}"
        sample.mkdir(exist_ok=True)
        original = sample / f"{900000 + number}.png"
        generated = sample / "result.png"
        with Image.open(matches[0]) as image:
            if image.size != (2552, 1223):
                raise ValueError("截图尺寸改变，裁剪范围需要重新目视核对")
            if not original.exists(): image.crop((23, 43, 1130, 1150)).save(original)
            if not generated.exists(): image.crop((1320, 43, 2147, 1150)).save(generated)
        product = next(p for p in scan_inputs(sample).products if p.product_id == str(900000 + number))
        candidate = inspect_image(product.product_id, f"user-sample-{number}", generated)
        manifest.append({"sample": number, "expected": "pass" if number == 3 else "reject",
                         "product": asdict(product), "candidate": asdict(candidate), "source_screenshot": str(matches[0])})
    target = root / "manifest.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"samples": len(manifest), "manifest": str(target)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
