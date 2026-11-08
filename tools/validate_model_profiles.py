"""隔离复验模型配置：读取人工样本清单，完整审图并统计真实调用，不修改业务目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.atomic_json import atomic_write_json
from soutui_image.codex_runtime import observe_calls
from soutui_image.review_images import ReviewImage, assert_review_image_unchanged
from soutui_image.review_profiles import SUPPORTED_MODELS, profile, review_cache_key, use_review_profile
from soutui_image.rule_runtime import use_rules
from soutui_image.rule_store import policy_id
from soutui_image.rules_bundle import builtin_bundle
from soutui_image.stage_a_inputs import ProductInput, assert_source_unchanged
from soutui_image.stage_e_codex_review import review_candidate


CRITICAL_FEATURES = ("袖口边缘结构", "门襟层次与边线", "扣位边缘与凸片", "裤长与脚踝位置")
STAGES = ("locator", "first", "verification", "buttons")
TOKEN_NOTE = "仅独立transport日志tokens used，不重复计上层副本；缺计数不按0；未细分输入/缓存/输出/推理，不等同账单或套餐额度。"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def plain_path(path):
    """拒绝链接目录；不让隔离验证输出经目录连接落到业务目录中。"""
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("验证路径不能包含目录链接")
    return path.resolve()


def load_plan(manifest_path, clarification_path=None):
    """只提取原图、候选和人工标签；旧AI响应不作为真值，也不传入新模型提示词。"""
    manifest_path = plain_path(manifest_path)
    manifest = read_json(manifest_path)
    product = ProductInput(**manifest["product"])
    assert_source_unchanged(product)
    protected = {str(manifest_path): sha256(manifest_path), str(Path(product.source_path).resolve()): product.sha256}
    samples = []
    seen_names, seen_results = set(), set()
    for number, item in enumerate(manifest["samples"], 1):
        name = item.get("sample", item.get("name", f"sample_{number}"))
        expected = item.get("decision", item.get("expected"))
        if not isinstance(name, str) or not name or name in seen_names or expected not in ("pass", "reject", "review"):
            raise ValueError("样本名称或人工结论无效/重复")
        candidate = ReviewImage(**item["candidate"])
        assert_review_image_unchanged(candidate)
        if candidate.product_id != product.product_id or candidate.result_id in seen_results:
            raise ValueError("样本商品不符或结果重复")
        seen_names.add(name); seen_results.add(candidate.result_id)
        protected[str(Path(candidate.local_path).resolve())] = candidate.sha256
        samples.append({"case_id": f"case_{number:03}", "sample": name, "expected": expected,
                        "feature_expectations": dict(item.get("feature_expectations", {})),
                        "candidate": asdict(candidate)})
    if not samples:
        raise ValueError("验证清单没有样本")
    if clarification_path:
        clarification_path = plain_path(clarification_path)
        clarification = read_json(clarification_path)
        protected[str(clarification_path)] = sha256(clarification_path)
        updates = clarification if isinstance(clarification, list) else [clarification]
        for update in updates:
            matches = [item for item in samples if item["sample"] == update["sample"]]
            if len(matches) != 1 or update["overall_expected"] not in ("pass", "reject", "review"):
                raise ValueError("人工澄清没有唯一对应样本或结论无效")
            matches[0]["expected"] = update["overall_expected"]
            matches[0]["feature_expectations"].update(update.get("feature_expectations", {}))
    for sample in samples:
        if any(k not in CRITICAL_FEATURES or v not in ("match", "mismatch", "uncertain", "not_applicable")
               for k, v in sample["feature_expectations"].items()):
            raise ValueError("人工专项标签无效")
    return product, samples, protected, manifest.get("rules_sha256")


def stage_for(path):
    parts = path.parts
    if "detail_location" in parts:
        return "locator"
    if any(value.startswith("button_focus") for value in parts):
        return "buttons"
    return "verification" if "verification" in parts else "first"


def transport_statistics(cache_root, chosen):
    """逐个独立transport统计，包括失败重试；上层stderr副本绝不重复累计。"""
    entries = []
    for log in sorted(cache_root.rglob("stderr.txt")) if cache_root.exists() else []:
        match = re.fullmatch(r"(transport_\d+)_(\d+)", log.parent.name)
        if not match:
            continue
        content = log.read_text(encoding="utf-8", errors="replace")
        timing_path = log.parent.parent / (match[1] + ".json")
        timing = next((x for x in read_json(timing_path) if x.get("attempt") == int(match[2])), {}) if timing_path.exists() else {}
        def header(name):
            found = re.search(r"(?m)^" + re.escape(name) + r":\s*([^\r\n]+)", content)
            return found[1].strip() if found else None
        tokens = re.search(r"(?m)^tokens used\s*\r?\n([\d,]+)\s*$", content)
        stage = stage_for(log.relative_to(cache_root))
        expected_effort = (chosen.locator_effort if stage == "locator" else
                           chosen.button_effort if stage == "buttons" else chosen.body_effort)
        model, provider, effort = header("model"), header("provider"), header("reasoning effort")
        ws_attempt = bool(re.search(r"wss://|WebSocket[^\r\n]*(?:failed|fallback|retry)|falling back[^\r\n]*WebSocket", content, re.I))
        entries.append({"stage": stage, "log": str(log.relative_to(cache_root)), "seconds": timing.get("seconds"),
                        "tokens_used": int(tokens[1].replace(",", "")) if tokens else None,
                        "returncode": timing.get("returncode"), "timeout": timing.get("timeout", False),
                        "actual_model": model, "actual_provider": provider, "actual_effort": effort,
                        "websocket_attempt_observed": ws_attempt,
                        "configuration_matches": model == chosen.model and provider == "openai_https"
                                                 and effort == expected_effort and not ws_attempt})
    by_stage = {}
    for stage in STAGES:
        rows = [x for x in entries if x["stage"] == stage]
        by_stage[stage] = {"attempts": len(rows), "seconds_sum": round(sum(x["seconds"] or 0 for x in rows), 3),
                           "missing_timing_count": sum(x["seconds"] is None for x in rows),
                           "tokens_used_sum": sum(x["tokens_used"] or 0 for x in rows),
                           "missing_token_count": sum(x["tokens_used"] is None for x in rows)}
    return {"attempts": entries, "by_stage": by_stage,
            "logged_tokens_used_sum": sum(x["tokens_used"] or 0 for x in entries),
            "missing_token_count": sum(x["tokens_used"] is None for x in entries),
            "configuration_verified": bool(entries) and all(x["configuration_matches"] for x in entries),
            "token_note": TOKEN_NOTE}


def critical_checks(review, expectations):
    """记录首审和复核的关键项差异；只有模型真实结论参与比对，不以人工覆盖伪装通过。"""
    checks = {}
    for feature in CRITICAL_FEATURES:
        stages = []
        for index, verdict in enumerate(review.passes):
            found = next(check for check in verdict["checks"] if check["feature"] == feature)
            stages.append({"stage": "first" if index == 0 else "verification", **found})
        checks[feature] = {"passes": stages, "changed_between_passes": len({x["comparison"] for x in stages}) > 1,
                           "expected": expectations.get(feature),
                           "matched": feature not in expectations or
                                      all(x["comparison"] == expectations[feature] for x in stages)}
    return checks


def run_sample(product, item, model_directory, bundle, chosen):
    """审核原始字节，只在独立验证目录创建缓存与证据，断点仍由正式E模块核验。"""
    candidate = ReviewImage(**item["candidate"])
    result_path = model_directory / (item["case_id"] + ".json")
    previous = read_json(result_path) if result_path.exists() else None
    if previous and (previous["sample"] != item["sample"] or previous["expected"] != item["expected"]
                     or previous["profile"] != chosen.to_dict() or previous["source_sha256"] != product.sha256
                     or previous["result_sha256"] != candidate.sha256):
        raise ValueError("样本验证目录已经绑定其他输入、标签或模型配置")
    events = []
    started = time.perf_counter()
    result = {"case_id": item["case_id"], "sample": item["sample"], "expected": item["expected"],
              "profile": chosen.to_dict(), "source_sha256": product.sha256, "result_sha256": candidate.sha256}
    with use_rules(bundle), use_review_profile(chosen):
        cache = model_directory / "reviews"
        cache_root = cache / review_cache_key(product, candidate)
        try:
            with observe_calls(lambda event: events.append({"at_monotonic": time.perf_counter(), **event})):
                reviewed = review_candidate(product, candidate, cache, model=chosen.model)
            result.update(actual=reviewed.decision, matched=reviewed.decision == item["expected"],
                          critical_checks=critical_checks(reviewed, item["feature_expectations"]), review=asdict(reviewed))
        except Exception as error:
            result.update(actual="error", matched=False, error_type=type(error).__name__, error=str(error))
        result["transport"] = transport_statistics(cache_root, chosen)
    elapsed = round(time.perf_counter() - started, 3)
    result["execution_seconds"] = (previous["execution_seconds"] if previous and previous.get("actual") != "error"
                                   and not events else elapsed)
    result["latest_invocation_seconds"] = elapsed
    result["invocation_seconds_total"] = round(elapsed + (previous.get("invocation_seconds_total", 0) if previous else 0), 3)
    result["events"] = [*(previous.get("events", []) if previous else []), *events]
    if previous and previous.get("actual") == "error":
        result["previous_errors"] = [*previous.get("previous_errors", []),
                                     {"type": previous.get("error_type"), "error": previous.get("error")}]
    result["source_after_sha256"] = sha256(product.source_path)
    result["result_after_sha256"] = sha256(candidate.local_path)
    result["inputs_unchanged"] = (result["source_after_sha256"] == product.sha256
                                   and result["result_after_sha256"] == candidate.sha256)
    result["verified"] = (result["matched"] and result["inputs_unchanged"]
                           and result["transport"]["configuration_verified"]
                           and all(x["matched"] for x in result.get("critical_checks", {}).values()))
    return result


def public_summary(results, bundle, protected_unchanged, *, complete):
    """明确列举可公开字段；不泄露商品ID、路径、图片哈希、人工描述或模型原文。"""
    models = {}
    for model, rows in results.items():
        stages = {}
        for stage in STAGES:
            stages[stage] = {key: round(sum(row["transport"]["by_stage"][stage][key] for row in rows), 3)
                             for key in ("attempts", "seconds_sum", "missing_timing_count", "tokens_used_sum", "missing_token_count")}
        cases = []
        for row in rows:
            cases.append({"case_id": row["case_id"], "expected": row["expected"], "actual": row["actual"],
                          "matched": row["matched"], "verified": row["verified"],
                          "execution_seconds": row["execution_seconds"], "inputs_unchanged": row["inputs_unchanged"],
                          "configuration_verified": row["transport"]["configuration_verified"],
                          "critical_comparisons": {feature: [x["comparison"] for x in data["passes"]]
                                                    for feature, data in row.get("critical_checks", {}).items()}})
        models[model] = {"profile": profile(model).to_dict(), "samples_finished": len(rows),
                         "matches": sum(row["matched"] for row in rows), "verified": sum(row["verified"] for row in rows),
                         "by_stage": stages, "cases": cases,
                         "logged_tokens_used_sum": sum(row["transport"]["logged_tokens_used_sum"] for row in rows),
                         "missing_token_count": sum(row["transport"]["missing_token_count"] for row in rows)}
    return {"status": "verified_samples" if complete and protected_unchanged and
                      all(row["verified"] for rows in results.values() for row in rows) else "needs_review" if complete else "running",
            "policy_version": policy_id(bundle), "rules_sha256": bundle.sha256, "models": models,
            "inputs_unchanged": protected_unchanged, "new_generation_submissions": 0, "token_note": TOKEN_NOTE,
            "scope": "仅所给人工样本的完整审核与真实调用测量；不代表跨款准确率，也不是新生图整批速度实测。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--clarification", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="+", choices=SUPPORTED_MODELS, default=list(SUPPORTED_MODELS))
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=2)
    args = parser.parse_args(argv)
    if len(set(args.models)) != len(args.models):
        raise ValueError("模型列表重复")
    product, samples, protected, manifest_rule_hash = load_plan(args.manifest, args.clarification)
    output = plain_path(args.output)
    # 禁止将记录写入业务图片/既有审图缓存目录，也禁止把源文件放进验证输出里。
    forbidden = {Path(product.source_path).resolve().parent / product.product_id}
    forbidden.update(Path(item["candidate"]["local_path"]).resolve().parent for item in samples)
    if any(output == path or output.is_relative_to(path) for path in forbidden):
        raise ValueError("必须使用独立验证目录，不能写入业务结果或审核缓存目录")
    if any(Path(path).is_relative_to(output) for path in protected):
        raise ValueError("验证输出目录不能包含源图或输入清单")
    bundle = builtin_bundle()
    config = {"schema_version": 1, "protected_sha256": protected, "samples": samples,
              "rules_sha256": bundle.sha256, "manifest_rules_sha256": manifest_rule_hash,
              "profile_schema": profile().to_dict()["schema_version"]}
    config_path = output / "private_run_config.json"
    if config_path.exists():
        if read_json(config_path) != config:
            raise ValueError("验证目录已绑定其他图片、规则或人工标签，请使用新目录")
    elif output.exists() and any(output.iterdir()):
        raise ValueError("验证目录非空且没有本工具运行配置，不能接管")
    output.mkdir(parents=True, exist_ok=True)
    if any(path.is_symlink() or getattr(path, "is_junction", lambda: False)() for path in output.rglob("*")):
        raise ValueError("验证目录内含链接，不能复用外部证据或输出")
    atomic_write_json(config_path, config)
    results = {}
    for model in args.models:
        chosen = profile(model)
        directory = plain_path(output / model)
        directory.mkdir(exist_ok=True)
        results[model] = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(run_sample, product, item, directory, bundle, chosen) for item in samples]
            for future in as_completed(futures):
                result = future.result()
                atomic_write_json(directory / (result["case_id"] + ".json"), result)
                results[model].append(result)
                results[model].sort(key=lambda x: x["case_id"])
                unchanged = all(sha256(path) == expected for path, expected in protected.items())
                atomic_write_json(output / "public_summary.json", public_summary(results, bundle, unchanged, complete=False))
                print(json.dumps({"model": model, "case": result["case_id"], "expected": result["expected"],
                                  "actual": result["actual"], "verified": result["verified"],
                                  "seconds": result["execution_seconds"],
                                  "logged_tokens": result["transport"]["logged_tokens_used_sum"]}, ensure_ascii=True), flush=True)
                if not unchanged:
                    raise ValueError("审核期间源图、候选或人工输入改变，停止验收")
    after = {path: sha256(path) for path in protected}
    unchanged = after == protected
    atomic_write_json(output / "private_integrity.json", {"before": protected, "after": after, "unchanged": unchanged,
                                                           "checked_at_utc": datetime.now(timezone.utc).isoformat()})
    summary = public_summary(results, bundle, unchanged, complete=True)
    atomic_write_json(output / "public_summary.json", summary)
    return 0 if summary["status"] == "verified_samples" else 2


if __name__ == "__main__":
    raise SystemExit(main())
