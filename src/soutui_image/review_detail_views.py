"""审核细节视图：只裁取原始像素并整数倍放大，绝不改写原图或交付照片。"""

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageOps


REGIONS = (
    ("upper_left", (0, 0, .45, .78)),
    ("upper_center", (.275, 0, .725, .78)),
    ("upper_right", (.55, 0, 1, .78)),
    ("lower_context", (0, .40, 1, 1)),
)


def prepare_detail_views(product, candidate, directory, *, regions=None):
    """输入绑定的原图、结果和证据目录；输出八个局部视图及裁剪/哈希清单。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    views = []
    for role, path, expected in (("original", product.source_path, product.sha256),
                                 ("generated", candidate.local_path, candidate.sha256)):
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("准备细节图时原图或结果内容已改变")
        with Image.open(path) as opened:
            picture = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = picture.size
            selected = REGIONS if regions is None else [(r["part"], tuple(v / 1000 for v in r["box"]))
                        for r in regions if r["role"] == role and r["visible"]]
            for index, (label, region) in enumerate(selected, 1):
                box = (int(region[0] * width), int(region[1] * height),
                       max(int(region[2] * width), int(region[0] * width) + 1),
                       max(int(region[3] * height), int(region[1] * height) + 1))
                detail = picture.crop(box)
                scale = min(4, max(1, (600 + detail.width - 1) // detail.width))
                if scale > 1:
                    detail = detail.resize((detail.width * scale, detail.height * scale), Image.Resampling.NEAREST)
                filename = f"{role}_{index:02}_{label}.png"
                output = target / filename
                detail.save(output)
                views.append({"role": role, "region": label, "source_sha256": expected,
                              "box_pixels": list(box), "integer_scale": scale,
                              "path": str(output.resolve()), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()})
    (target / "manifest.json").write_text(json.dumps({"pixel_changes": "仅裁切、EXIF 方向规范和最近邻整数倍放大",
                                                     "views": views}, ensure_ascii=False, indent=2), encoding="utf-8")
    return views


def assert_detail_views_unchanged(views):
    """模型调用前后核对附加视图，不能把后来改动的局部图当作原审核输入。"""
    for view in views:
        if hashlib.sha256(Path(view["path"]).read_bytes()).hexdigest() != view["sha256"]:
            raise ValueError("细节视图内容改变，审核结果不能作为通过")
