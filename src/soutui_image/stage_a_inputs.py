"""板块 A：只读整理白底图片，建立商品 ID、图片路径及内容校验值的对应关系。

本板块只扫描所选文件夹的第一层，不读取商品输出子目录，不创建或改写商品图片。
独立验证入口：python -m soutui_image.stage_a_inputs <白底图文件夹> --report <报告文件>
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import warnings
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError


SUPPORTED_SUFFIXES = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}
MAX_SOURCE_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class ProductInput:
    """一张原始商品图；ID 保留为字符串，避免丢失前导零。"""

    product_id: str
    source_path: str
    width: int
    height: int
    image_format: str
    sha256: str


@dataclass(frozen=True)
class InputIssue:
    """输入异常；调用方必须展示原因，不能悄悄遗漏商品。"""

    file_name: str
    reason: str


@dataclass(frozen=True)
class InputBatch:
    """本次输入清单；存在异常时不得直接启动整批网站操作。"""

    input_directory: str
    products: tuple[ProductInput, ...]
    issues: tuple[InputIssue, ...]

    @property
    def ready(self) -> bool:
        """至少有一个有效商品且没有待处理的输入异常，才可开始后续流程。"""
        return bool(self.products) and not self.issues

    def to_dict(self) -> dict:
        """转换为可保存的中文友好 JSON 报告。路径在运行时解析，不绑定开发电脑。"""
        return {"schema_version": 1, **asdict(self), "ready": self.ready}


def _read_product(path: Path) -> ProductInput:
    """从同一份字节读取图片尺寸和哈希，检查损坏、格式与过大文件。"""
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("原图超过 50 MB，请先确认图片文件")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("原图超过 50 MB，请先确认图片文件")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as picture:
            image_format = picture.format
            if image_format not in set(SUPPORTED_SUFFIXES.values()):
                raise ValueError("图片真实格式不是支持的 JPEG 或 PNG")
            width, height = picture.size
            picture.verify()
        # verify 检查文件结构，load 进一步确认完整像素可解码。
        with Image.open(io.BytesIO(raw)) as picture:
            picture.load()
    # 部分商家素材扩展名与编码不一致，记录真实格式供上传板块使用，不改动原文件。
    return ProductInput(path.stem, str(path), width, height, image_format, hashlib.sha256(raw).hexdigest())


def scan_inputs(directory: str | Path) -> InputBatch:
    """扫描当前目录的 JPG/JPEG/PNG 原图，识别重复 ID 及无效图片。

    返回结果中有 issues 时 ready=False；重复 ID 的所有图片均不加入有效商品，
    防止同一 ID 的 JPG 与 PNG 被误用成两个商品，或静默覆盖其中一张。
    """
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"请选择白底图文件夹：{root}")
    candidates = sorted(
        (p for p in root.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES and not p.is_dir()),
        key=lambda p: p.name.casefold(),
    )
    counts = Counter(p.stem for p in candidates)
    products: list[ProductInput] = []
    issues: list[InputIssue] = []
    for path in candidates:
        if path.is_symlink() or path.resolve().parent != root:
            issues.append(InputIssue(path.name, "原图不能是链接到其他目录的文件"))
            continue
        if re.fullmatch(r"[0-9]+", path.stem) is None:
            issues.append(InputIssue(path.name, "文件名必须是商品 ID（仅数字，不含扩展名）"))
            continue
        if counts[path.stem] > 1:
            issues.append(InputIssue(path.name, "同一商品 ID 有多张原图，请明确保留哪一张"))
            continue
        try:
            products.append(_read_product(path))
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as error:
            issues.append(InputIssue(path.name, f"无法使用该图片：{error}"))
    if not candidates:
        issues.append(InputIssue("", "当前目录没有 JPG、JPEG 或 PNG 原图；不会递归读取输出子目录"))
    return InputBatch(str(root), tuple(products), tuple(issues))


def assert_source_unchanged(product: ProductInput) -> None:
    """后续板块上传前可调用，确保使用的原图未在扫描后被更换。"""
    actual = hashlib.sha256(Path(product.source_path).read_bytes()).hexdigest()
    if actual != product.sha256:
        raise ValueError(f"商品 {product.product_id} 的原图已变化，请重新扫描")


def main(argv: list[str] | None = None) -> int:
    """板块 A 独立入口；只有明确指定 --report 时才写报告，不改动图片。"""
    parser = argparse.ArgumentParser(description="板块 A：扫描白底图并验证商品 ID")
    parser.add_argument("directory", help="白底图文件夹完整路径")
    parser.add_argument("--report", type=Path, help="将输入验证报告保存为 JSON")
    args = parser.parse_args(argv)
    try:
        batch = scan_inputs(args.directory)
        report = json.dumps(batch.to_dict(), ensure_ascii=False, indent=2)
        if args.report:
            destination = args.report.expanduser().resolve()
            if destination.suffix.lower() != ".json":
                raise ValueError("验证报告必须使用 .json 扩展名，不能覆盖图片")
            if destination.exists():
                raise FileExistsError(f"报告已存在，请指定新文件名：{destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8") as handle:
                handle.write(report + "\n")
        print(report)
        return 0 if batch.ready else 2
    except (OSError, ValueError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
