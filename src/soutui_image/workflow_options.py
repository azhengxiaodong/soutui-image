"""商品任务固定执行模式：新任务默认只生图，已开始任务续跑保持原模型和审核设置。"""

import json
from pathlib import Path

from .atomic_json import atomic_write_json
from .review_profiles import ReviewProfile, legacy_profile, profile
from .stage_a_inputs import assert_source_unchanged
from .generation_settings import validate_resolution, existing_resolution


def options_path(product, tasks):
    root = Path(tasks).resolve()
    path = root / f"product_{product.product_id}_workflow.json"
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("任务配置路径越界")
    return path


def read_options(product, tasks):
    """只读固定配置，未登记的旧生成记录识别为原 Sol/xhigh 完整审图模式。"""
    path = options_path(product, tasks)
    prior_resolution = existing_resolution(product, tasks)
    if path.exists():
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("schema_version") != 1 or record.get("product_id") != product.product_id
                or record.get("source_sha256") != product.sha256
                or type(record.get("review_enabled")) is not bool):
            raise ValueError("任务配置与当前原图不一致")
        ReviewProfile.from_dict(record["review_profile"])
        # 0.2.8 及以前没有尺寸字段，正式请求一直是 2K；只读补齐，不改旧配置。
        resolution = validate_resolution(record.get("resolution", "2K"))
        if prior_resolution is not None and prior_resolution != resolution:
            raise ValueError("任务配置与已有生成记录的分辨率不一致")
        record["resolution"] = resolution
        return record
    if prior_resolution is not None:
        return {"schema_version": 1, "product_id": product.product_id, "source_sha256": product.sha256,
                "review_enabled": True, "review_profile": legacy_profile().to_dict(), "legacy": True,
                "resolution": prior_resolution}
    return None


def bind_options(product, tasks, *, review_enabled=False, selected_profile=None, resolution="2K"):
    """明确开始时保存一次；界面默认值或后来改选模型不会改变正在续跑的任务。"""
    assert_source_unchanged(product)
    resolution = validate_resolution(resolution)
    old = read_options(product, tasks)
    selected = selected_profile or profile()
    record = old or {"schema_version": 1, "product_id": product.product_id, "source_sha256": product.sha256,
                     "review_enabled": bool(review_enabled), "review_profile": selected.to_dict(), "legacy": False,
                     "resolution": resolution}
    path = options_path(product, tasks)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, record)
    return record
