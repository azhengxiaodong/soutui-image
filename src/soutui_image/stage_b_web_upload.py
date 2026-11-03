"""板块 B 的独立接口变体：Codex 分类后，只上传一次当前原图并校验远端字节。

不改动原来已经冻结的 B 界面代码。接口方案的项目由本机任务记录维护；
这里保存商品、分类、原图哈希与服务端 URL，供后续九组任务共用，不生成图片。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .codex_clothing import ClothingClassification
from .stage_a_inputs import ProductInput, assert_source_unchanged
from .web_transport import WebTransport


OSS_HOST = "wxhook.oss-cn-shenzhen.aliyuncs.com"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


@dataclass(frozen=True)
class WebUploadReceipt:
    product_id: str
    source_path: str
    source_sha256: str
    category: str
    image_url: str
    image_format: str
    uploaded_bytes: int
    remote_sha256: str
    upload_seconds: float


def validate_upload_urls(signed_url, public_url):
    """只向网页实际使用的 HTTPS 存储桶上传，不把网站登录令牌发给图片服务器。"""
    if not isinstance(signed_url, str) or not isinstance(public_url, str):
        raise ValueError("上传签名缺少图片地址")
    signed, public = urllib.parse.urlsplit(signed_url), urllib.parse.urlsplit(public_url)
    for item in (signed, public):
        if (item.scheme != "https" or item.netloc != OSS_HOST or item.username or item.password
                or not item.path.startswith("/inputs/") or item.fragment):
            raise ValueError("上传目标不在已经验证的原站点图片存储范围内")
    if signed.path != public.path or public.query:
        raise ValueError("上传与下载地址未对应同一图片，或公开地址带有临时签名")


def verify_remote_image(receipt: WebUploadReceipt, transport: WebTransport) -> None:
    """不带网站会话请求新上传的原图，确认没有改色、压缩或串图。"""
    validate_upload_urls(receipt.image_url, receipt.image_url)
    request = urllib.request.Request(receipt.image_url, headers={"User-Agent": "soutui-image/0.1"})
    try:
        with transport.opener.open(request, timeout=transport.timeout) as response:
            raw = response.read(MAX_UPLOAD_BYTES + 1)
    except (urllib.error.URLError, OSError):
        raise RuntimeError("无法验证已上传原图，未提交后续生成") from None
    actual = hashlib.sha256(raw).hexdigest()
    if len(raw) != receipt.uploaded_bytes or actual != receipt.source_sha256 or actual != receipt.remote_sha256:
        raise ValueError("远端图片与当前原图字节不符，未提交后续生成")


def upload_product(product: ProductInput, classification: ClothingClassification, directory,
                   session_path, *, transport=None) -> WebUploadReceipt:
    """上传原图并保存成功回执；已有回执重跑时只读复核，不重复上传。"""
    assert_source_unchanged(product)
    if (classification.product_id != product.product_id or classification.source_sha256 != product.sha256
            or classification.category not in {"套装", "上装", "下装"}):
        raise ValueError("分类不属于当前商品原图，未上传")
    source = Path(product.source_path)
    raw = source.read_bytes()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("接口原图超过 15 MB；暂不自动压缩或修改用户原图")
    # 再检查同一份待传字节，避免扫描与读取之间原图被替换。
    if hashlib.sha256(raw).hexdigest() != product.sha256:
        raise ValueError("待上传图片在读取时发生变化")
    root = Path(directory).resolve()
    target = root / f"product_{product.product_id}_upload.json"
    client = transport or WebTransport(session_path)
    if target.exists():
        record = json.loads(target.read_text(encoding="utf-8"))
        receipt = WebUploadReceipt(**record["receipt"])
        if (receipt.product_id != product.product_id or receipt.source_sha256 != product.sha256
                or receipt.category != classification.category
                or Path(receipt.source_path).resolve() != source.resolve()):
            raise ValueError("已有上传记录不属于当前原图，未覆盖")
        verify_remote_image(receipt, client)
        return receipt
    mime = {"JPEG": "image/jpeg", "PNG": "image/png"}.get(product.image_format)
    if not mime:
        raise ValueError("接口只上传已验证的 JPEG/PNG 原图")
    extension = ".jpg" if product.image_format == "JPEG" else ".png"
    start = time.perf_counter()
    signing = client.request_json("GET", "/api/web/oss/sign-url", query={
        "filename": product.product_id + extension, "contentType": mime})
    if signing.get("success") is not True:
        raise RuntimeError("网站没有成功签发原图上传地址")
    signed_url, public_url = signing.get("signedUrl"), signing.get("publicUrl")
    validate_upload_urls(signed_url, public_url)
    headers = {"Content-Type": mime}
    cache_control = signing.get("cacheControl")
    if cache_control:
        if not isinstance(cache_control, str) or any(c in cache_control for c in "\r\n"):
            raise ValueError("上传缓存参数无效")
        headers["Cache-Control"] = cache_control
    # OSS 使用网站签发的临时 URL，自身请求不携带网站 Authorization。
    request = urllib.request.Request(signed_url, data=raw, headers=headers, method="PUT")
    try:
        with client.opener.open(request, timeout=client.timeout) as response:
            response.read(1024)
    except (urllib.error.URLError, OSError):
        raise RuntimeError("原图上传未确认成功，未自动重发，未提交生成") from None
    receipt = WebUploadReceipt(product.product_id, product.source_path, product.sha256,
                               classification.category, public_url, product.image_format, len(raw),
                               product.sha256, round(time.perf_counter() - start, 3))
    verify_remote_image(receipt, client)
    assert_source_unchanged(product)
    root.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        json.dump({"status": "uploaded_and_verified", "receipt": asdict(receipt),
                   "classification": asdict(classification)}, handle, ensure_ascii=False, indent=2)
    return receipt
