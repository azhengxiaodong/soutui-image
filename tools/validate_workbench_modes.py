"""用本机真实图片和完整审核缓存隔离回放两种模式，不调用网站或模型。

例：python -B tools/validate_workbench_modes.py --product 商品ID --row 3
只有输出与记录生命周期属于本次验收；查询顺序是受控回放，既有审核结论
不是新模型/新强度的准确率证据，也不用于推算实际生图或审图耗时。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from soutui_image.atomic_json import atomic_write_json
from soutui_image.generation_only import run_generation_only
from soutui_image.output_store import _no_links, read_manifest
from soutui_image.pipeline import Pipeline, PipelinePaths
from soutui_image.review_images import inspect_image
from soutui_image.review_profiles import legacy_profile
from soutui_image.review_record_validation import validate_bound_review
from soutui_image.rule_runtime import current_policy
from soutui_image.saved_images import counts
from soutui_image.saved_review import run_saved_review
from soutui_image.stage_a_inputs import _read_product
from soutui_image.stage_c_web_plan import load_plan
from soutui_image.workflow_options import bind_options


def sha(path):
    """流式计算原字节哈希；不重新编码图片。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def protected_snapshot(before):
    """核对交付前登记的全部业务文件及真实图片；输出辅助文件迁移不混入此范围。"""
    expected = {str((PROJECT / name).resolve()): digest for name, digest in before["business"].items()}
    for name, digest in before["output_files"].items():
        if Path(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            expected[str(Path(name).resolve())] = digest
    actual = {name: sha(name) if Path(name).is_file() else None for name in expected}
    changed = [name for name in expected if actual[name] != expected[name]]
    return actual, changed


def contained(path, root):
    """删除或复制前核对最终路径且拒绝目录链接。"""
    resolved = _no_links(Path(path))
    boundary = _no_links(Path(root))
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise ValueError("隔离回放路径越界")
    return resolved


def validate(product_id, row, evidence_dir):
    before_path = PROJECT / "validation/workbench_028/before.json"
    before = load(before_path)
    baseline, changes = protected_snapshot(before)
    if changes:
        raise ValueError(f"回放前已有 {len(changes)} 份受保护业务文件与登记不符，停止")
    plan_path = PROJECT / "data/tasks" / f"product_{product_id}_references.json"
    job_path = PROJECT / "data/tasks" / product_id / f"group_{row:02}" / "round_00.json"
    report_path = PROJECT / "data/review_cache" / product_id / f"group_{row:02}" / "round_00/round_review.json"
    original_plan = load_plan(plan_path)
    original_product = _read_product(Path(original_plan.source_path))
    original_job, report = load(job_path), load(report_path)
    if (original_product.product_id != product_id or original_plan.source_sha256 != original_product.sha256
            or not report.get("all_successful_images_reviewed") or report.get("status") != "reviewed"
            or report["source_sha256"] != original_product.sha256
            or report["task_id"] != original_job["task_id"] or len(report["outcomes"]) != 5):
        raise ValueError("原图、真实任务和完整审核缓存未绑定")
    outcomes = {item["candidate"]["result_id"]: item for item in report["outcomes"]}
    decisions = [item["review"]["decision"] for item in outcomes.values()]
    if set(decisions) != {"pass", "reject", "review"}:
        raise ValueError("回放组须同时包含实际通过、拒绝、待确认缓存")
    evidence_sources = []
    for item in outcomes.values():
        raw = item["review"]
        validate_bound_review(original_product, item["candidate"], raw)
        if raw.get("review_profile") is not None or raw["policy_version"] != current_policy():
            raise ValueError("本工具只回放当前规则的原 Sol/xhigh 完整缓存")
        image = Path(item["candidate"]["local_path"])
        if sha(image) != item["candidate"]["sha256"]:
            raise ValueError("真实缓存图片哈希已变化")
        evidence = Path(raw["evidence_directory"])
        files = sorted(p for p in evidence.rglob("*.json") if p.is_file())
        if not files:
            raise ValueError("真实审核原响应证据缺失")
        evidence_sources.append({"result_id": raw["result_id"], "image_path": str(image),
                                 "sourceSHA": sha(image), "review_sha256": object_sha(raw),
                                 "decision": raw["decision"], "passes": len(raw["passes"]),
                                 "evidence_files": {str(p): sha(p) for p in files}})

    evidence_dir = contained(evidence_dir, PROJECT)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="mode_replay_", dir=evidence_dir))
    contained(workspace, evidence_dir)
    ledger = []
    events = []
    forbidden = []
    first_saved = threading.Event()
    query_observations = []
    cached_calls = []
    details = {"scope": "真实图片与既有完整审核缓存的离线隔离回放",
               "limitations": ["网站完成顺序由回放控制，不是新生图验收", "未调用模型，不验证新模型或新强度准确率",
                               "回放耗时不代表真实生图或审图耗时", "拒绝及待确认标签沿用已有 AI 记录，不新增人工真值"],
               "before_snapshot_sha256": sha(before_path), "product_id": product_id,
               "row": row, "policy_version": current_policy(), "review_profile": legacy_profile().to_dict(),
               "source_inputs": {str(p): sha(p) for p in (plan_path, job_path, report_path)},
               "real_review_sources": evidence_sources, "copies": ledger,
               "protected_file_count": len(baseline), "protected_snapshot_sha256": object_sha(baseline)}

    def copy_image(source, destination):
        destination = contained(destination, workspace)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = sha(source)
        shutil.copyfile(source, destination)
        if sha(destination) != digest:
            raise ValueError("隔离图片复制校验失败")
        ledger.append({"source": str(source), "copy": str(destination), "sourceSHA": digest,
                       "verified_after_copy": True})
        return destination

    def deny(name):
        def blocked(*args, **kwargs):
            forbidden.append(name)
            raise AssertionError("离线验收禁止调用：" + name)
        return blocked

    try:
        source = copy_image(original_product.source_path, workspace / "inputs" / Path(original_product.source_path).name)
        product = replace(original_product, source_path=str(source))
        plan = replace(original_plan, source_path=str(source))
        paths = PipelinePaths(workspace / "data")
        paths.tasks.mkdir(parents=True)
        atomic_write_json(paths.tasks / plan_path.name, plan.to_dict())
        # 先固定新任务的关闭审核选项，随后才放入隔离初轮记录，避免被当作历史旧任务。
        bind_options(product, paths.tasks, review_enabled=False)
        isolated_job = paths.tasks / product_id / f"group_{row:02}" / "round_00.json"
        isolated_job.parent.mkdir(parents=True)
        unfinished = copy.deepcopy(original_job)
        unfinished.update(state="generating", successful_results=1, failed_results=0)
        for result in unfinished["results"][1:]:
            result.update(status="pending", image_url=None)
        atomic_write_json(isolated_job, unfinished)

        def on_event(event):
            events.append(copy.deepcopy(event))
            if event["kind"] == "generated_saved" and not first_saved.is_set():
                state = load(isolated_job)
                details["first_saved_observation"] = {"server_state": state["state"],
                    "successful_slots": sum(r["status"] == "done" for r in state["results"]),
                    "pending_slots": sum(r["status"] == "pending" for r in state["results"]),
                    "total_saved": event["total_saved"]}
                first_saved.set()

        pipeline = Pipeline(paths, on_event=on_event, review_enabled=False)

        def query(path, client):
            complete = first_saved.is_set()
            returned = copy.deepcopy(original_job if complete else unfinished)
            atomic_write_json(path, returned)
            query_observations.append({"after_first_saved": complete, "state": returned["state"]})
            return returned

        def download(pid, result, root):
            item = outcomes[result["result_id"]]
            target = copy_image(item["candidate"]["local_path"], root / Path(item["candidate"]["local_path"]).name)
            return inspect_image(pid, result["result_id"], target)

        def cached_review(current_product, candidate, directory, *, model):
            raw = copy.deepcopy(outcomes[candidate.result_id]["review"])
            validate_bound_review(current_product, candidate, raw)
            if model != "gpt-6.1-sol" or sha(candidate.local_path) != raw["result_sha256"]:
                raise ValueError("缓存回放的模型或图片身份不符")
            cached_calls.append({"result_id": candidate.result_id, "sha256": candidate.sha256,
                                 "review_sha256": object_sha(raw), "decision": raw["decision"]})
            return raw

        with ExitStack() as stack:
            for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                           "urllib.request.urlopen", "urllib.request.OpenerDirector.open", "subprocess.Popen",
                           "soutui_image.codex_runtime.run_image_call", "soutui_image.stage_e_codex_review.review_candidate",
                           "soutui_image.pipeline.review_candidate", "soutui_image.generation_only.prepare_job",
                           "soutui_image.generation_only.submit_job", "soutui_image.stage_d_web_generation.prepare_job",
                           "soutui_image.stage_d_web_generation.submit_job", "soutui_image.stage_d_web_generation.query_job"):
                stack.enter_context(patch(target, side_effect=deny(target)))
            stack.enter_context(patch("soutui_image.generation_only.query_job", side_effect=query))
            stack.enter_context(patch("soutui_image.generation_only.download_result", side_effect=download))
            audit_entry = stack.enter_context(patch("soutui_image.saved_review.review_candidate", side_effect=deny("E审图入口")))
            generated = run_generation_only(pipeline, product, plan, None, [plan.assignments[row - 1]], None,
                                            poll_interval=.01, generation_timeout=15)
            output = source.parent / product_id
            manifest_before = read_manifest(output, paths.tasks, product)
            tally_before = counts(manifest_before)[row]
            assert tally_before == {"saved": 5, "pass": 0, "review": 0, "unreviewed": 5}, tally_before
            assert details["first_saved_observation"] == {"server_state": "generating", "successful_slots": 1,
                                                          "pending_slots": 4, "total_saved": 1}
            assert audit_entry.call_count == 0 and not paths.reviews.exists()
            assert len(list(paths.tasks.rglob("round_*.json"))) == 1
            for entry in manifest_before["groups"][row - 1]["saved"]:
                target = output / f"组{row:02}" / entry["filename"]
                original = Path(outcomes[entry["result_id"]]["candidate"]["local_path"])
                assert sha(target) == sha(original) == entry["sha256"]
                ledger.append({"source": str(original), "copy": str(target), "sourceSHA": sha(original),
                               "verified_after_copy": True, "saved_by_application": True})
            assert not (output / ".soutui.lock").exists() and not (output / ".搜推生图.json").exists()
            details["generation_only"] = {"result": generated, "counts": tally_before,
                "E_calls": audit_entry.call_count, "new_generation_submissions": 0,
                "extra_rounds": 0, "query_observations": query_observations,
                "manifest": manifest_before, "output_has_auxiliary_files": False}

            # 使用原 Sol/xhigh 配置消费原始完整缓存；绝不改写为新配置验收。
            audit_entry.side_effect = cached_review
            reviewed = run_saved_review(pipeline, product, legacy_profile())
            manifest_after = read_manifest(output, paths.tasks, product)
            tally_after = counts(manifest_after)[row]
            expected = {"saved": decisions.count("pass") + decisions.count("review"),
                        "pass": decisions.count("pass"), "review": decisions.count("review"), "unreviewed": 0}
            assert reviewed["status"] == "complete" and reviewed["reviewed"] == 5 and reviewed["errors"] == 0, reviewed
            assert tally_after == expected, tally_after
            for entry in manifest_before["groups"][row - 1]["saved"]:
                target = output / f"组{row:02}" / entry["filename"]
                decision = outcomes[entry["result_id"]]["review"]["decision"]
                assert target.exists() == (decision != "reject")
                if target.exists():
                    assert sha(target) == entry["sha256"]
            assert len(cached_calls) == 5 and len(list(paths.tasks.rglob("round_*.json"))) == 1 and not forbidden
            details["saved_review"] = {"result": reviewed, "counts": tally_after, "cache_calls": cached_calls,
                                      "deleted": decisions.count("reject"), "manifest": manifest_after}
            details["events"] = events
            details["forbidden_calls"] = forbidden
            details["passed"] = True
    finally:
        # 原图和每份尚存复制图先逐项验哈希；已拒绝图片只能由已记录删除事务解释。
        completed_deletions = {}
        if "output" in locals() and output.exists():
            cleanup_manifest = read_manifest(output, paths.tasks, product)
            for deletion in cleanup_manifest.get("removed", []):
                if deletion.get("deletion_status") == "complete":
                    validate_bound_review(product, deletion, deletion["audit_review"])
                    assert deletion["audit_review"]["decision"] == "reject"
                    target = output / f"组{deletion['row']:02}" / deletion["filename"]
                    completed_deletions[str(target)] = deletion["sha256"]
        removed = {str(Path(item["copy"])) for item in ledger if item.get("saved_by_application")
                   and not Path(item["copy"]).exists()}
        known_hashes = {item["sourceSHA"] for item in ledger}
        for item in ledger:
            assert sha(item["source"]) == item["sourceSHA"], "回放源图片变化"
            target = Path(item["copy"])
            item["verified_before_cleanup"] = target.exists() and sha(target) == item["sourceSHA"]
            item["deleted_by_review"] = (str(target) in removed
                                         and completed_deletions.get(str(target)) == item["sourceSHA"])
            assert item["verified_before_cleanup"] or item["deleted_by_review"], "复制图丢失或变化"
        remaining_images = [p for p in workspace.rglob("*") if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
        for path in workspace.rglob("*"):
            contained(path, workspace)
        assert all(sha(path) in known_hashes for path in remaining_images), "隔离目录出现未知图片"
        cleanup_count = sum(p.is_file() for p in workspace.rglob("*"))
        contained(workspace, evidence_dir)
        shutil.rmtree(workspace)
        after, changes = protected_snapshot(before)
        details["cleanup"] = {"copied_image_entries": len(ledger), "rejected_copies_deleted_during_review": len(removed),
            "remaining_images_verified_and_deleted": len(remaining_images), "temporary_files_deleted": cleanup_count,
            "workspace_removed": not workspace.exists(), "original_protected_files_unchanged": after == baseline,
            "changed_protected_files": changes}
        atomic_write_json(evidence_dir / "mode_replay_private.json", details)
        assert after == baseline and not changes, "原业务文件发生变化"

    public = {"schema_version": 1, "passed": details.get("passed", False),
              "scope": "真实图片及既有完整审核缓存的离线隔离回放", "limitations": details["limitations"],
              "generation_only": {"saved": 5, "approved": 0, "unreviewed": 5,
                  "saved_before_other_four_finished": True, "original_bytes_verified": True,
                  "E_calls": 0, "new_generation_submissions": 0, "extra_rounds": 0},
              "saved_review": {"real_cached_reviews": 5, "retained_pass": decisions.count("pass"),
                  "retained_pending_confirmation": decisions.count("review"), "deleted_reject": decisions.count("reject"),
                  "new_model_calls": 0, "new_generation_submissions": 0, "retained_original_bytes_verified": True},
              "network_or_process_attempts": len(forbidden), "protected_file_count": len(baseline),
              "protected_files_unchanged": details["cleanup"]["original_protected_files_unchanged"],
              "output_auxiliary_files": 0, "copied_image_entries": len(ledger),
              "test_workspace_removed": not workspace.exists(),
              "private_evidence_sha256": sha(evidence_dir / "mode_replay_private.json"),
              "validator_sha256": sha(__file__)}
    atomic_write_json(PROJECT / "validation/public_handoff/workbench_modes_replay_028.json", public)
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True)
    parser.add_argument("--row", type=int, required=True, choices=range(1, 10))
    args = parser.parse_args()
    if not args.product.isdigit():
        parser.error("商品 ID 必须全为数字")
    result = validate(args.product, args.row, PROJECT / "validation/workbench_028")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
