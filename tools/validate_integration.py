"""只读核验真实九组闭环：关联、固定参数、逐图证据、补生上限与最终文件内容。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.stage_e_codex_review import POLICY_VERSION, validate_review


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify(data, pid, *, allow_in_progress=False):
    """输入现有任务目录和商品 ID；输出可复核统计，不发请求、不调用模型。"""
    plan = read(data / "tasks" / f"product_{pid}_references.json")
    source = Path(plan["source_path"])
    original_hash = digest(source)
    assert original_hash == plan["source_sha256"], "原图已改变"
    groups = plan["assignments"]
    assert [g["row"] for g in groups] == list(range(1, 10)), "不是完整九组"
    assert len({g["folder_id"] for g in groups}) == 9, "参考组有重复"
    output = source.parent / pid
    manifest = read(output / ".搜推生图.json")
    assert manifest["product_id"] == pid and manifest["source_sha256"] == original_hash
    assert sorted(p.name for p in output.iterdir() if p.is_dir()) == [f"组{i:02}" for i in range(1, 10)]
    saved_groups = {g["row"]: g for g in manifest["groups"]}
    rows, task_ids, image_ids = [], set(), set()
    pending, original_url = [], None
    generated = reviewed = rejected = uncertain = quoted = initial_reviewed = initial_failed = generation_failed = 0
    for group in groups:
        row = group["row"]
        refs = [r["image_id"] for r in group["references"]]
        urls = [r["url"] for r in group["references"]]
        assert len(refs) == len(set(refs)) == 5
        saved = saved_groups[row]
        assert (saved["folder_id"], saved["reference_ids"]) == (group["folder_id"], refs)
        jobs = sorted((data / "tasks" / pid / f"group_{row:02}").glob("round_*.json"))
        approved, passes_by_id, round_by_id, rounds = set(), {}, {}, []
        assert len(jobs) <= 4, "超出初轮加三轮补生上限"
        for index, path in enumerate(jobs):
            job = read(path)
            assert job["round_index"] == index, "补生轮次不连续"
            assert job["expected_results"] == 5, "不是每组五张结果位"
            assert len(approved) < 3, "已达标后仍提交了新轮次"
            assert (job["product_id"], job["source_sha256"], job["row"], job["folder_id"], job["reference_ids"]) == (
                pid, original_hash, row, group["folder_id"], refs), "任务与原图/参考组关联错误"
            assert job["task_id"] not in task_ids, "重复任务号"
            task_ids.add(job["task_id"])
            params = job["payload"]["params"]
            original_url = original_url or params["inputImageUrls"]
            assert params["inputImageUrls"] == original_url, "补生更换了原图"
            assert params["inputImageUrls2"] == urls, "补生更换了参考图"
            assert job["payload"]["featureType"] == "model-dressing"
            assert (params["aspectRatio"], params["resolution"], params["extraParams"]["imageTier"],
                    params["extraParams"]["resultsPerReference"], params["extraParams"]["selectedPoseCount"]) == (
                        "3:4", "2K", "balanced", "1", "5")
            if job.get("submit_attempted_at"): quoted += job["quoted_credits"]
            successful = {r["result_id"]: r for r in job.get("results", []) if r["status"] == "done"}
            if job["state"] in ("awaiting_audit", "generation_failed"):
                assert len(successful) + job["failed_results"] == 5, "本轮结果位不完整"
                assert len({r["result_id"] for r in job["results"]}) == 5, "本轮结果 ID 重复"
            generated += len(successful)
            generation_failed += job.get("failed_results", 0)
            if index == 0: initial_failed += job.get("failed_results", 0)
            report_path = data / "review_cache" / pid / f"group_{row:02}" / f"round_{index:02}" / "round_review.json"
            if not report_path.exists():
                pending.append(f"组{row:02}轮{index:02}未完成整轮审核")
                rounds.append({"round": index, "state": job["state"], "successful": len(successful), "reviewed": 0})
                continue
            report = read(report_path)
            assert report["all_successful_images_reviewed"] and report["task_id"] == job["task_id"]
            assert (report["source_sha256"], report["folder_id"], report["reference_ids"]) == (original_hash, group["folder_id"], refs)
            outcomes = report["outcomes"]
            assert len(outcomes) == len(successful)
            assert {o["candidate"]["result_id"] for o in outcomes} == set(successful), "有成功图漏审"
            for outcome in outcomes:
                candidate, review = outcome["candidate"], outcome["review"]
                rid = candidate["result_id"]
                assert rid not in image_ids, "结果图重复审核/串组"
                image_ids.add(rid)
                result = successful[rid]
                assert outcome["reference_id"] == result["reference_id"] == refs[outcome["reference_index"]]
                assert candidate["sha256"] == digest(candidate["local_path"]) == review["result_sha256"]
                assert (review["product_id"], review["result_id"], review["source_sha256"], review["policy_version"]) == (
                    pid, rid, original_hash, POLICY_VERSION)
                for evidence in review["passes"]:
                    validate_review(evidence, pid, rid)
                assert review["passes"][-1]["decision"] == review["decision"]
                if review["decision"] == "pass":
                    assert len(review["passes"]) == 2 and all(p["decision"] == "pass" for p in review["passes"])
                    approved.add(candidate["sha256"]); passes_by_id[rid] = outcome
                    round_by_id[rid] = index
                elif review["decision"] == "reject": rejected += 1
                else: uncertain += 1
            reviewed += len(outcomes)
            if index == 0: initial_reviewed += len(outcomes)
            rounds.append({"round": index, "state": "reviewed", "successful": len(successful), "reviewed": len(outcomes),
                           "pass": report["pass_count"], "reject": report["reject_count"], "review": report["review_count"]})
        hashes = [item["sha256"] for item in saved["saved"]]
        assert len(hashes) == len(set(hashes)), "组内存在重复内容"
        assert set(hashes) <= approved, "保存了未通过的图片"
        if set(hashes) != approved: pending.append(f"组{row:02}有已通过图片尚未保存")
        directory = output / saved["directory"]
        assert {p.name for p in directory.iterdir() if p.is_file()} == {i["filename"] for i in saved["saved"]}
        for item in saved["saved"]:
            assert digest(directory / item["filename"]) == item["sha256"], "保存文件与审核图片字节不符"
            assert passes_by_id[item["result_id"]]["candidate"]["sha256"] == item["sha256"]
            assert passes_by_id[item["result_id"]]["reference_id"] == item["reference_id"]
            assert round_by_id[item["result_id"]] == item["round_index"]
        state = "complete" if len(hashes) >= 3 else "paused" if len(rounds) == 4 and rounds[-1]["state"] == "reviewed" else "in_progress"
        if state == "in_progress": pending.append(f"组{row:02}仍在处理")
        rows.append({"row": row, "group": group["group_name"], "approved_saved": len(hashes),
                     "retry_rounds": max(0, len(jobs) - 1), "state": state, "rounds": rounds})
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "product_id": pid, "source_sha256": original_hash,
              "original_unchanged": True, "nine_unique_groups": True, "all_saved_files_match_double_pass_evidence": True,
              "initial_expected": 45, "initial_reviewed": initial_reviewed, "initial_generation_failed": initial_failed,
              "generation_failed": generation_failed, "queried_successful_results": generated,
              "reviewed": reviewed, "rejected": rejected, "uncertain": uncertain, "saved": sum(r["approved_saved"] for r in rows),
              "quoted_submitted_credits": quoted, "groups": rows, "pending": pending,
              "status": "verified" if not pending else "in_progress"}
    if not allow_in_progress:
        assert initial_reviewed + initial_failed == 45, "四十五个初轮结果位未全部追踪"
        assert not pending, "完整联调尚有未完成项：" + "; ".join(pending)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product_id")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-in-progress", action="store_true")
    args = parser.parse_args()
    result = verify(args.data_dir, args.product_id, allow_in_progress=args.allow_in_progress)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "groups"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
