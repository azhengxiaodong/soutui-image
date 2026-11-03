"""核对真实两商品第一组串联：不串原图、不串审核及保存；第二款其余八组未执行。"""

import hashlib
import json
from pathlib import Path

from validate_integration import verify

ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    data = ROOT / "data"
    identifiers = ["1083284353580", "1084307568457"]
    run = json.loads((data / "last_run_result.json").read_text(encoding="utf-8"))
    assert [p["product_id"] for p in run["products"]] == identifiers
    assert all([g["row"] for g in p["groups"]] == [1] for p in run["products"])
    assert all(p["groups"][0]["state"] in ("complete", "paused") for p in run["products"])
    results = [verify(data, identifiers[0]), verify(data, identifiers[1], allow_in_progress=True)]
    assert len({r["source_sha256"] for r in results}) == 2
    assert results[0]["saved"] == 32
    assert results[1]["groups"][0]["state"] in ("complete", "paused")
    assert all(g["approved_saved"] == 0 and not g["rounds"] for g in results[1]["groups"][1:])
    mappings = []
    for pid in identifiers:
        plan = json.loads((data / "tasks" / f"product_{pid}_references.json").read_text(encoding="utf-8"))
        original = Path(plan["source_path"])
        actual_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        jobs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((data / "tasks" / pid / "group_01").glob("round_*.json"))]
        assert jobs and all(j["product_id"] == pid and j["source_sha256"] == actual_hash for j in jobs)
        urls = {j["payload"]["params"]["inputImageUrls"][0] for j in jobs}
        assert len(urls) == 1
        mappings.append({"product_id": pid, "source_sha256": actual_hash, "original_url": next(iter(urls)),
                         "task_ids": [j["task_id"] for j in jobs], "group01_saved": results[identifiers.index(pid)]["groups"][0]["approved_saved"]})
    assert mappings[0]["original_url"] != mappings[1]["original_url"]
    assert set(mappings[0]["task_ids"]).isdisjoint(mappings[1]["task_ids"])
    events = [json.loads(line) for line in (data / "last_run.events.jsonl").read_text(encoding="utf-8").splitlines()]
    start = max(i for i, e in enumerate(events) if e["kind"] == "started" and e.get("products") == 2)
    submissions = [e for e in events[start:] if e["kind"] == "submitting"]
    assert all(e["product_id"] == identifiers[1] and e["row"] == 1 for e in submissions)
    report = {"status": "verified", "scope": "两商品第一组串联，第一款复用既有结果，第二款第一组真实生成/审核/保存",
              "product_ids": identifiers, "source_hashes_distinct": True, "task_original_urls_distinct": True,
              "first_product_32_images_preserved": True, "first_product_new_submissions": 0,
              "second_product_other_eight_groups_executed": False, "mappings": mappings, "run_result": run}
    target = ROOT / "validation" / "integration"
    (target / "multi_product_row01.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "multi_product_events.json").write_text(json.dumps(events[start:], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("mappings", "run_result")}, ensure_ascii=True))
