"""板块 E：每张结果分别与当前商品原图比对，拟通过的图片再做一次独立复核。

使用本机已登录的 Codex CLI，无需另购模型 API。只核对生成图实际可见的服装，
没有展示的部位不参与判定；可见证据不清才待复核，明确款式改变必须拒绝。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .rule_runtime import active_rules, current_policy
from .review_schedule import run_independent_checks

from .codex_clothing import find_codex
from .review_images import ReviewImage, assert_review_image_unchanged
from .review_detail_views import prepare_detail_views, assert_detail_views_unchanged
from .review_detail_locator import locate_detail_regions
from .review_button_edges import review_button_edges
from .review_checkpoints import fingerprint, save_checkpoint, load_checkpoint, digest
from .stage_a_inputs import ProductInput, assert_source_unchanged


POLICY_VERSION = "2026-11-03.3"
LEGACY_FEATURES = ("颜色", "花纹与面料", "服装组成与版型", "领口与门襟", "袖子与袖口",
                   "上衣下摆", "裤子与裤脚", "口袋", "扣子与配件", "其他变化")
FEATURES = (*LEGACY_FEATURES, "袖口边缘结构", "门襟层次与边线", "扣位边缘与凸片", "裤长与脚踝位置")
VISIBILITY = ("visible", "partial", "not_visible")
COMPARISONS = ("match", "mismatch", "uncertain", "not_applicable")
CHECK_SCHEMA = {"type": "object", "properties": {
    "feature": {"type": "string", "enum": list(FEATURES)},
    "visibility": {"type": "string", "enum": list(VISIBILITY)},
    "comparison": {"type": "string", "enum": list(COMPARISONS)},
    "original": {"type": "string"}, "generated": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["feature", "visibility", "comparison", "original", "generated", "reason"], "additionalProperties": False}
REVIEW_SCHEMA = {"type": "object", "properties": {
    "product_id": {"type": "string"}, "result_id": {"type": "string"},
    "decision": {"type": "string", "enum": ["pass", "reject", "review"]},
    "visible_scope": {"type": "string"}, "unshown_parts": {"type": "array", "items": {"type": "string"}},
    "checks": {"type": "array", "items": CHECK_SCHEMA}, "summary": {"type": "string"}},
    "required": ["product_id", "result_id", "decision", "visible_scope", "unshown_parts", "checks", "summary"],
    "additionalProperties": False}


@dataclass(frozen=True)
class ImageReview:
    policy_version: str
    product_id: str
    result_id: str
    source_sha256: str
    result_sha256: str
    decision: str
    summary: str
    visible_scope: str
    passes: tuple[dict, ...]
    evidence_directory: str


def validate_review(data: dict, product_id: str, result_id: str) -> dict:
    """校验逐项证据与最终结论相符，不把模型随意写的 pass 直接当作通过。"""
    if not isinstance(data, dict) or set(data) != set(REVIEW_SCHEMA["required"]):
        raise ValueError("审核响应字段不完整")
    if data["product_id"] != product_id or data["result_id"] != result_id:
        raise ValueError("审核对象不属于当前商品和图片")
    if not isinstance(data["checks"], list) or len(data["checks"]) != len(FEATURES):
        raise ValueError("审核没有覆盖完整服装检查项")
    checks = data["checks"]
    if {c.get("feature") for c in checks} != set(FEATURES):
        raise ValueError("审核检查项缺失或重复")
    visible = []
    for check in checks:
        if not isinstance(check, dict) or set(check) != set(CHECK_SCHEMA["required"]):
            raise ValueError("审核检查项格式不符")
        if check["visibility"] not in VISIBILITY or check["comparison"] not in COMPARISONS:
            raise ValueError("可见范围或比较结论无效")
        if not all(isinstance(check[k], str) and check[k].strip() for k in ("original", "generated", "reason")):
            raise ValueError("审核缺少原图、生成图或差异依据")
        if check["visibility"] == "not_visible":
            if check["comparison"] != "not_applicable":
                raise ValueError("没有展示的部位不能声称通过、拒绝或不确定")
        elif check["comparison"] == "not_applicable":
            raise ValueError("实际展示的检查项不能直接跳过；均不存在的特征可说明后判相同")
        else:
            visible.append(check)
    if not visible:
        raise ValueError("审核没有实际可见服装的判断")
    expected = ("reject" if any(c["comparison"] == "mismatch" for c in visible)
                else "review" if any(c["comparison"] == "uncertain" for c in visible) else "pass")
    if data["decision"] != expected:
        raise ValueError("总审核结论与可见部分逐项证据不一致，需要重新审核")
    if not all(isinstance(data[k], str) and data[k].strip() for k in ("visible_scope", "summary")):
        raise ValueError("审核缺少可见范围与总体依据")
    if not isinstance(data["unshown_parts"], list) or not all(isinstance(x, str) for x in data["unshown_parts"]):
        raise ValueError("未展示部位列表格式无效")
    return data


def _prompt(product: ProductInput, candidate: ReviewImage, *, verification=False, views=()):
    attachments = "\n".join(f"附件{i}: {'原图' if v['role'] == 'original' else '生成图'}的{v['region']}，原像素框={v['box_pixels']}" for i, v in enumerate(views, 3))
    return active_rules().render("body", product_id=product.product_id, result_id=candidate.result_id,
        review_kind="独立复核" if verification else "首次审核", attachments=attachments,
        features_json=json.dumps(FEATURES,ensure_ascii=False))


def _audit_once(product, candidate, attempt: Path, *, codex_exe=None, model=None, verification=False, timeout=600, parallel_checks=True):
    model = model or "gpt-6.1-sol"
    attempt.mkdir(parents=True, exist_ok=True)
    schema, response = attempt / "schema.json", attempt / "response.json"
    if schema.exists():
        if json.loads(schema.read_text(encoding="utf-8"))!=REVIEW_SCHEMA:
            raise ValueError("旧审核阶段的结构化规则不符")
    else:
        schema.write_text(json.dumps(REVIEW_SCHEMA, ensure_ascii=False), encoding="utf-8")
    cli = find_codex(codex_exe)
    regions = locate_detail_regions(product, candidate, attempt.parent / "detail_location", codex_exe=codex_exe, model=model, timeout=timeout)
    manifest = attempt / "detail_views" / "manifest.json"
    views = (json.loads(manifest.read_text(encoding="utf-8"))["views"] if manifest.exists() else
             prepare_detail_views(product, candidate, attempt / "detail_views", regions=regions))
    assert_detail_views_unchanged(views)
    for view in views:
        expected = product.sha256 if view["role"]=="original" else candidate.sha256
        if view["source_sha256"]!=expected:
            raise ValueError("断点局部图不属于当前原图或结果")
    prompt = _prompt(product,candidate,verification=verification,views=views)
    identity = {"policy_version":current_policy(),"product_id":product.product_id,"result_id":candidate.result_id,
                "source_sha256":product.sha256,"result_sha256":candidate.sha256,"model":model,
                "verification":verification,"prompt":fingerprint(prompt),"schema":fingerprint(REVIEW_SCHEMA),
                "views":[v["sha256"] for v in views]}
    checkpoint=attempt/"body_checkpoint.json"
    verdict=None
    if checkpoint.exists():
        verdict=validate_review(load_checkpoint(checkpoint,identity),product.product_id,candidate.result_id)
    elif response.exists():
        # 旧程序只有在整图响应校验成功后才进入下一阶段；目录存在与原日志共同证明它确实已完成。
        progressed=(any(attempt.glob("button_focus*")) if verification else (attempt.parent/"verification/schema.json").exists())
        log=attempt/"stderr.txt"
        if progressed and log.exists():
            actual=log.read_text(encoding="utf-8")
            if prompt.strip() in actual and f"model: {model}" in actual:
                verdict=validate_review(json.loads(response.read_text(encoding="utf-8")),product.product_id,candidate.result_id)
                save_checkpoint(checkpoint,identity,verdict,response=response)
    def run_body():
        nonlocal verdict, schema, response
        if verdict is None:
            call_directory=attempt
            if response.exists() or (attempt/"stderr.txt").exists() or any(attempt.glob("body_attempt_*")):
                # 未完成的调用保留原日志，在同一阶段的新尝试目录续做，不能覆盖上次错误证据。
                call_directory=attempt/("body_attempt_"+str(time.time_ns()))
                call_directory.mkdir()
                schema=call_directory/"schema.json"
                schema.write_text(json.dumps(REVIEW_SCHEMA,ensure_ascii=False),encoding="utf-8")
                response=call_directory/"response.json"
            command=[str(cli),"exec","--ephemeral","--sandbox","read-only","--skip-git-repo-check",
                     "--image",product.source_path,"--image",candidate.local_path]
            for view in views:
                command += ["--image",view["path"]]
            command += ["--output-schema",str(schema),"--output-last-message",str(response),"--model",model,"-"]
            try:
                result=subprocess.run(command,input=prompt,text=True,encoding="utf-8",errors="replace",
                                      capture_output=True,cwd=call_directory,timeout=timeout,
                                      creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            except subprocess.TimeoutExpired:
                raise TimeoutError("Codex 图片审核超时，该图未通过，不能进入合格目录") from None
            (call_directory/"stdout.txt").write_text(result.stdout,encoding="utf-8")
            (call_directory/"stderr.txt").write_text(result.stderr,encoding="utf-8")
            if result.returncode!=0 or not response.exists():
                raise RuntimeError(f"Codex 图片审核未成功，返回码 {result.returncode}；证据：{call_directory}")
            verdict=validate_review(json.loads(response.read_text(encoding="utf-8")),product.product_id,candidate.result_id)
            assert_source_unchanged(product)
            assert_review_image_unchanged(candidate)
            assert_detail_views_unchanged(views)
            save_checkpoint(checkpoint,identity,verdict,response=response)
        assert_source_unchanged(product)
        assert_review_image_unchanged(candidate)
        assert_detail_views_unchanged(views)
        return verdict

    def run_buttons():
        # E：拟通过者除整图复核外，再看分段扣位。专项不确定也不能进入合格目录。
        # 使用审核规则版本作为稳定标识；打包后的模块位于PYZ中，不能要求源码.py存在。
        button_identity={**identity,"implementation":"button-context:"+current_policy()}
        button_checkpoint=attempt/"button_checkpoint.json"
        check=None
        if button_checkpoint.exists():
            stored=json.loads(button_checkpoint.read_text(encoding="utf-8"))
            if stored.get("identity")==button_identity:
                check=load_checkpoint(button_checkpoint,button_identity)
            else:
                history=attempt/"checkpoint_history"; history.mkdir(exist_ok=True)
                backup=history/("button_"+digest(button_checkpoint)+".json")
                if not backup.exists(): backup.write_bytes(button_checkpoint.read_bytes())
        if check is None:
            focus=attempt/"button_focus"
            if focus.exists(): focus=attempt/("button_focus_"+str(time.time_ns()))
            check = review_button_edges(product, candidate, views, focus, model=model,
                                        codex_exe=codex_exe, timeout=timeout)
            save_checkpoint(button_checkpoint,button_identity,check,response=focus/"response.json")
        return check

    # 两项都看同一份原始图片；专项不读取整图结论，并行不减少任何质检步骤。
    visible_placket = any(v["role"] == "generated" and v["region"] == "门襟与扣列" for v in views)
    verdict, check, timing = run_independent_checks(
        run_body, run_buttons if verification and visible_placket else None, parallel=parallel_checks)
    if verification and next(c for c in verdict["checks"] if c["feature"] == "扣位边缘与凸片")["visibility"] != "not_visible":
        if check is None:
            check = run_buttons()
        verdict = combine_button_check(verdict, check, product.product_id, candidate.result_id)
        (attempt / "combined_response.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    (attempt / "timing.json").write_text(json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")
    return verdict


def combine_button_check(verdict, check, product_id, result_id):
    """把真实专项证据纳入复核；任何一方发现错误或不确定均不得被另一方的相同覆盖。"""
    import copy
    from .review_button_edges import validate_button_check
    validate_review(verdict, product_id, result_id)
    validate_button_check(check)
    result = copy.deepcopy(verdict)
    old = next(c for c in result["checks"] if c["feature"] == check["feature"])
    # 专项裁剪没有展示而整图已展示时保留整图，不能通过裁剪把可见错误隐藏掉。
    rank = {"not_applicable": -1, "match": 0, "uncertain": 1, "mismatch": 2}
    if rank[check["comparison"]] >= rank[old["comparison"]]:
        result["checks"][result["checks"].index(old)] = copy.deepcopy(check)
    comparisons = {c["comparison"] for c in result["checks"] if c["visibility"] != "not_visible"}
    result["decision"] = "reject" if "mismatch" in comparisons else "review" if "uncertain" in comparisons else "pass"
    labels = {"pass": "复核通过", "review": "待复核", "reject": "复核不通过"}
    result["summary"] = labels[result["decision"]] + "；整图：" + result["summary"] + "；扣位分段专项：" + check["reason"]
    return validate_review(result, product_id, result_id)


def review_candidate(product: ProductInput, candidate: ReviewImage, directory, *, codex_exe=None, model=None):
    """每图单独审核；首次拟通过者必须独立复核也通过，缓存绑定两张图片哈希与规则版本。"""
    assert_source_unchanged(product)
    model = model or "gpt-6.1-sol"
    assert_review_image_unchanged(candidate)
    if candidate.product_id != product.product_id:
        raise ValueError("结果图片不属于当前商品")
    identity = "\0".join((current_policy(), product.product_id, candidate.result_id,
                          product.sha256, candidate.sha256, model or "default"))
    key = hashlib.sha256(identity.encode()).hexdigest()
    root = Path(directory).resolve() / key
    result_path = root / "review.json"
    if result_path.exists():
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        report = ImageReview(**{**raw, "passes": tuple(raw["passes"])})
        if (report.policy_version != current_policy() or report.product_id != product.product_id
                or report.result_id != candidate.result_id or report.source_sha256 != product.sha256
                or report.result_sha256 != candidate.sha256):
            raise ValueError("已存审核结论与当前图片不符")
        for item in report.passes:
            validate_review(item, product.product_id, candidate.result_id)
        expected = report.passes[-1]["decision"]
        if expected != report.decision or (expected == "pass" and
                (len(report.passes) != 2 or any(p["decision"] != "pass" for p in report.passes))):
            raise ValueError("缓存缺少通过图片的独立复核")
        return report
    root.mkdir(parents=True, exist_ok=True)
    previous=sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()),key=lambda p:int(p.name))
    run=root/str(time.time_ns())
    if previous:
        latest=previous[-1]
        if (latest/"detail_location/located.json").exists() or (latest/"first/body_checkpoint.json").exists() or ((latest/"first/response.json").exists() and
                (latest/"verification/schema.json").exists()):
            run=latest
    first = _audit_once(product, candidate, run / "first", codex_exe=codex_exe, model=model)
    passes = [first]
    if first["decision"] == "pass":
        passes.append(_audit_once(product, candidate, run / "verification", codex_exe=codex_exe, model=model, verification=True))
    last = passes[-1]
    report = ImageReview(current_policy(), product.product_id, candidate.result_id, product.sha256,
                         candidate.sha256, last["decision"], last["summary"], last["visible_scope"], tuple(passes), str(run))
    with result_path.open("x", encoding="utf-8") as handle:
        json.dump(asdict(report), handle, ensure_ascii=False, indent=2)
    return report
