"""生成尺寸校验：只支持用户可选的 2K/4K，并保持已有商品所有轮次一致。"""

import json
from pathlib import Path


RESOLUTIONS = ("2K", "4K")


def validate_resolution(value):
    """校验明确尺寸；未知值不降级为 2K，不把图片放大当作后台 4K 生成。"""
    if not isinstance(value, str) or value not in RESOLUTIONS:
        raise ValueError("生图分辨率只支持 2K 或 4K")
    return value


def job_resolution(record):
    """从持久任务的真实请求参数读取尺寸；缺失或损坏的记录必须先核对。"""
    try:
        value = record["payload"]["params"]["resolution"]
    except (KeyError, TypeError):
        raise ValueError("已有生成记录缺少有效的分辨率参数，未猜测或覆盖") from None
    return validate_resolution(value)


def existing_resolution(product, tasks):
    """只读核对商品所有组和轮次；prepared/未知提交也须保留原分辨率。"""
    root = Path(tasks).resolve()
    values = set()
    for path in sorted((root / product.product_id).glob("group_*/round_*.json")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("旧生成记录路径越界，未绑定任务配置")
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("product_id") != product.product_id
                or record.get("source_sha256") != product.sha256):
            raise ValueError("旧生成记录与当前商品或原图不一致，未绑定任务配置")
        values.add(job_resolution(record))
    if len(values) > 1:
        raise ValueError("已有商品各组或轮次的分辨率不一致，未修改或继续提交")
    return next(iter(values), None)
