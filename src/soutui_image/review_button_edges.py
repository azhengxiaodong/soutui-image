"""扣位专项：把过长的门襟分段展示，单独核对每个扣位与扣间的自由边。"""

import hashlib
import json
import subprocess
from pathlib import Path

from .rule_runtime import active_rules, current_policy

from PIL import Image, ImageOps

from .codex_clothing import find_codex
from .review_detail_views import assert_detail_views_unchanged
from .review_images import assert_review_image_unchanged
from .stage_a_inputs import assert_source_unchanged


SCHEMA = {"type": "object", "properties": {
    "feature": {"type": "string", "enum": ["扣位边缘与凸片"]},
    "visibility": {"type": "string", "enum": ["visible", "partial", "not_visible"]},
    "comparison": {"type": "string", "enum": ["match", "mismatch", "uncertain", "not_applicable"]},
    "original": {"type": "string"}, "generated": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["feature", "visibility", "comparison", "original", "generated", "reason"], "additionalProperties": False}


def validate_button_check(data):
    """专项仍区分可见与未展示，不把模型返回的任意文字当通过依据。"""
    if not isinstance(data, dict) or set(data) != set(SCHEMA["required"]) or data["feature"] != "扣位边缘与凸片":
        raise ValueError("扣位专项响应不完整")
    if (data["visibility"] not in ("visible", "partial", "not_visible") or
            data["comparison"] not in ("match", "mismatch", "uncertain", "not_applicable")):
        raise ValueError("扣位专项结论无效")
    if (data["visibility"] == "not_visible") != (data["comparison"] == "not_applicable"):
        raise ValueError("扣位专项可见性与结论矛盾")
    if not all(isinstance(data[k], str) and data[k].strip() for k in ("original", "generated", "reason")):
        raise ValueError("扣位专项缺少逐颗比较证据")
    return data


def prepare_button_segments(views, directory, *, product=None, candidate=None):
    """输入已绑定的原像素局部；沿门襟等分三段，减少细长图片整体缩放导致的细节丢失。"""
    assert_detail_views_unchanged(views)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    segments = []
    for view in views:
        if view["region"] != "门襟与扣列":
            continue
        native = product is not None and candidate is not None
        source_path = (product.source_path if view["role"] == "original" else candidate.local_path) if native else view["path"]
        with Image.open(source_path) as opened:
            picture = ImageOps.exif_transpose(opened).convert("RGB")
            native_box = [0, 0, picture.width, picture.height]
            if native:
                expected = product.sha256 if view["role"] == "original" else candidate.sha256
                if view["source_sha256"] != expected or hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != expected:
                    raise ValueError("扣位上下文与审核原图不符")
                left, top, right, bottom = view["box_pixels"]
                # 两侧至少增加原门襟框一倍宽度，保留衣料和折边，不能只跟踪一条白色缝线。
                padding = max(right - left, int(picture.width * .06))
                native_box = [max(0, left - padding), top, min(picture.width, right + padding), bottom]
                picture = picture.crop(native_box)
            for index in range(3):
                # 相邻段保留10%重叠，不能把刚好落在分界处的扣子切断。
                top = max(0, int(picture.height * (index / 3 - .05)))
                bottom = min(picture.height, int(picture.height * ((index + 1) / 3 + .05)))
                detail = picture.crop((0, top, picture.width, max(top + 1, bottom)))
                scale = min(4, max(1, (600 + detail.width - 1) // detail.width)) if native else 2 if detail.width < 400 else 1
                if scale > 1:
                    detail = detail.resize((detail.width * scale, detail.height * scale), Image.Resampling.NEAREST)
                path = target / f"{view['role']}_placket_{index + 1}.png"
                detail.save(path)
                segments.append({"role": view["role"], "part": index + 1, "path": str(path.resolve()),
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                 "parent_view_sha256": view["sha256"], "box_in_parent": [0, top, picture.width, bottom],
                                 "integer_scale": scale, "native_context_box": native_box,
                                 "source_sha256": view.get("source_sha256")})
    (target / "manifest.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    return segments


def review_button_edges(product, candidate, views, directory, *, model=None, codex_exe=None, timeout=600):
    """真实调用当前指定模型，仅审核扣位边缘；不读取上次审核的结论或期望标签。"""
    assert_source_unchanged(product)
    assert_review_image_unchanged(candidate)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
    segments = prepare_button_segments(views, root / "segments", product=product, candidate=candidate)
    attachments = "\n".join(f"附件{i}: {'原图' if s['role'] == 'original' else '生成图'}门襟第{s['part']}段" for i, s in enumerate(segments, 3))
    prompt = active_rules().render("buttons",attachments=attachments)
    schema, response = root / "schema.json", root / "response.json"
    schema.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    command = [str(find_codex(codex_exe)), "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
               "--image", product.source_path, "--image", candidate.local_path]
    for segment in segments:
        command += ["--image", segment["path"]]
    command += ["--output-schema", str(schema), "--output-last-message", str(response)]
    if model:
        command += ["--model", model]
    command.append("-")
    try:
        result = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace", capture_output=True,
                                cwd=root, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise TimeoutError("扣位专项复核超时，缺少完整通过证据") from None
    (root / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (root / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0 or not response.exists():
        raise RuntimeError(f"扣位专项审核失败；证据：{root}")
    assert_source_unchanged(product)
    assert_review_image_unchanged(candidate)
    assert_detail_views_unchanged(views)
    for segment in segments:
        if hashlib.sha256(Path(segment["path"]).read_bytes()).hexdigest() != segment["sha256"]:
            raise ValueError("扣位分段图片已改变")
    return validate_button_check(json.loads(response.read_text(encoding="utf-8")))
