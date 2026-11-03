"""审核素材缓存：下载原尺寸结果并核对内容，缓存和最终合格目录严格分开。"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

from .stage_b_web_upload import OSS_HOST
from .web_session import _NoRedirect


MAX_RESULT_BYTES = 30 * 1024 * 1024


@dataclass(frozen=True)
class ReviewImage:
    product_id: str
    result_id: str
    local_path: str
    sha256: str
    width: int
    height: int
    image_format: str
    byte_count: int


def inspect_image(product_id: str, result_id: str, path: str | Path) -> ReviewImage:
    """读取同一份字节计算哈希和解码，不改写图片像素。"""
    source = Path(path).resolve(strict=True)
    if source.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError("审核图片超过 30 MB")
    raw = source.read_bytes()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as picture:
            width, height = picture.size
            encoding = picture.format
            if encoding not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("审核结果格式不是 JPEG/PNG/WEBP")
            picture.verify()
        with Image.open(io.BytesIO(raw)) as picture:
            picture.load()
    return ReviewImage(product_id, result_id, str(source), hashlib.sha256(raw).hexdigest(),
                       width, height, encoding, len(raw))


def assert_review_image_unchanged(candidate: ReviewImage):
    if hashlib.sha256(Path(candidate.local_path).read_bytes()).hexdigest() != candidate.sha256:
        raise ValueError("待审核结果图已变化，需要重新下载和审核")


def download_result(product_id: str, result: dict, directory) -> ReviewImage:
    """只下载本轮成功结果到程序缓存；不带网站会话请求 OSS，不进入用户合格目录。"""
    if result.get("status") != "done" or not isinstance(result.get("result_id"), str):
        raise ValueError("只有明确成功且有结果身份的图片才进入审核")
    url = result.get("image_url")
    parsed = urllib.parse.urlsplit(url or "")
    if parsed.scheme != "https" or parsed.netloc != OSS_HOST or parsed.username or parsed.password:
        raise ValueError("结果图片不在已经验证的原站点存储范围")
    root = Path(directory).resolve()
    key = hashlib.sha256((product_id + "\0" + result["result_id"]).encode()).hexdigest()[:24]
    metadata = root / f"{key}.json"
    if metadata.exists():
        saved = ReviewImage(**json.loads(metadata.read_text(encoding="utf-8")))
        if (saved.product_id != product_id or saved.result_id != result["result_id"]
                or not Path(saved.local_path).resolve().is_relative_to(root)):
            raise ValueError("缓存结果与当前商品或结果身份不对应")
        assert_review_image_unchanged(saved)
        return saved
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "soutui-image/0.1"}), timeout=30) as response:
            raw = response.read(MAX_RESULT_BYTES + 1)
    except (urllib.error.URLError, OSError):
        raise RuntimeError("结果图下载失败，未审核、未保存为合格图") from None
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError("结果图超过审核下载大小限制")
    root.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(raw)) as picture:
        extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(picture.format)
    if not extension:
        raise ValueError("下载结果的真实图片格式不支持")
    target = root / (key + extension)
    temporary = target.with_suffix(target.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(raw)
    # 完整解码后才把临时文件变成正式缓存，便于中断后继续，不留下半张可审核图片。
    inspect_image(product_id, result["result_id"], temporary)
    if target.exists() and hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(raw).digest():
        raise ValueError("同一结果身份已有不同缓存内容，停止以免覆盖")
    temporary.replace(target)
    candidate = inspect_image(product_id, result["result_id"], target)
    with metadata.open("x", encoding="utf-8") as handle:
        json.dump(asdict(candidate), handle, ensure_ascii=False, indent=2)
    return candidate
