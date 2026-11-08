"""审核局部定位：只找袖口、门襟和下肢的位置，不给出商品合格结论。"""

import json
import subprocess
from .codex_runtime import run_image_call
from pathlib import Path

from .rule_runtime import active_rules, current_policy

from .codex_clothing import find_codex
from .review_images import assert_review_image_unchanged
from .stage_a_inputs import assert_source_unchanged
from .review_checkpoints import fingerprint
from .review_profiles import current_profile, phase_profile_identity, reasoning_arguments


PARTS = ("左侧袖口", "右侧袖口", "门襟与扣列", "裤腿与下肢")
REGION_SCHEMA = {"type": "object", "properties": {
    "role": {"type": "string", "enum": ["original", "generated"]},
    "part": {"type": "string", "enum": list(PARTS)},
    "visible": {"type": "boolean"},
    "box": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
    "description": {"type": "string"}},
    "required": ["role", "part", "visible", "box", "description"], "additionalProperties": False}
SCHEMA = {"type": "object", "properties": {
    "regions": {"type": "array", "items": REGION_SCHEMA}}, "required": ["regions"], "additionalProperties": False}


def validate_regions(data):
    """输入定位 JSON；输出严格限定在图片内的八个框，不能重复或用非法坐标裁图。"""
    if not isinstance(data, dict) or set(data) != {"regions"} or not isinstance(data["regions"], list):
        raise ValueError("局部定位结果格式错误")
    regions = data["regions"]
    expected = {(role, part) for role in ("original", "generated") for part in PARTS}
    if len(regions) != 8 or {(r.get("role"), r.get("part")) for r in regions} != expected:
        raise ValueError("局部定位未覆盖原图和结果的全部指定部位")
    for region in regions:
        if set(region) != set(REGION_SCHEMA["required"]) or type(region["visible"]) is not bool:
            raise ValueError("局部定位缺少可见性证据")
        box = region["box"]
        if (not isinstance(box, list) or len(box) != 4 or
                not all(type(v) is int and 0 <= v <= 1000 for v in box)):
            raise ValueError("局部定位坐标越界")
        if region["visible"] and not (box[0] < box[2] and box[1] < box[3]):
            raise ValueError("可见部位的定位框为空")
        if not isinstance(region["description"], str) or not region["description"].strip():
            raise ValueError("局部定位缺少说明")
    return sorted(regions, key=lambda r: (("original", "generated").index(r["role"]), PARTS.index(r["part"])))


def locate_detail_regions(product, candidate, directory, *, codex_exe=None, model=None, timeout=600):
    """仅通过整图定位；同一审核的两次质检共用裁剪坐标，不共享质检结论。"""
    chosen = current_profile(model)
    model = chosen.model
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    cached = root / "located.json"
    prompt = active_rules().render("locator")
    identity = {"source_sha256": product.sha256, "result_sha256": candidate.sha256, "model": model}
    if not chosen.legacy:
        identity.update(policy_version=current_policy(), prompt=fingerprint(prompt), schema=fingerprint(SCHEMA),
                        **phase_profile_identity(model=model))
    if cached.exists():
        data = json.loads(cached.read_text(encoding="utf-8"))
        if any(data.get(k) != v for k, v in identity.items()):
            raise ValueError("定位结果与实际图片不符")
        return validate_regions({"regions": data["regions"]})
    schema, response = root / "schema.json", root / "response.json"
    schema.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    command = [str(find_codex(codex_exe)), "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
               "--image", product.source_path, "--image", candidate.local_path,
               "--output-schema", str(schema), "--output-last-message", str(response)]
    if model:
        command += ["--model", model]
    command += reasoning_arguments("locator", model=model)
    command.append("-")
    try:
        result = run_image_call(command, stage="局部定位", input=prompt, text=True, encoding="utf-8", errors="replace",
                                capture_output=True, cwd=root, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise TimeoutError("Codex 局部定位超时，未取得完整审核证据") from None
    (root / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (root / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0 or not response.exists():
        raise RuntimeError(f"Codex 局部定位失败；证据：{root}")
    assert_source_unchanged(product)
    assert_review_image_unchanged(candidate)
    regions = validate_regions(json.loads(response.read_text(encoding="utf-8")))
    cached.write_text(json.dumps({**identity, "regions": regions}, ensure_ascii=False, indent=2), encoding="utf-8")
    return regions
