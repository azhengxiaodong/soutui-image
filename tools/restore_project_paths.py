"""重装后迁移活动记录的路径字段，不改任务身份、图片、审核结论或提示词指纹。"""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"source_path", "local_path", "path", "evidence_directory", "response_path", "report_path", "job_path", "input_directory"}


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mapping = {"D:\\搜推生图": ROOT}
    for path in (ROOT / "data/tasks").glob("product_*_upload.json"):
        receipt = json.loads(path.read_text(encoding="utf-8"))["receipt"]
        original = Path(receipt["source_path"])
        restored = ROOT / "商品图片" / original.name
        if sha(restored) != receipt["source_sha256"]:
            raise ValueError("迁移原图哈希不符")
        mapping[str(original.parent)] = ROOT / "商品图片"
    files = [ROOT / "data/settings.json"]
    for directory in ("data/tasks", "data/review_cache", "data/reviews"):
        files.extend(sorted((ROOT / directory).rglob("*.json")))
    prepared = []
    unresolved = []
    for path in files:
        # 原始模型响应保持原字节，确保 checkpoint.response_sha256 不变。
        if path.name == "response.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        changes = []

        def migrate(value, field=""):
            if isinstance(value, dict):
                return {k: migrate(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [migrate(v, field) for v in value]
            if isinstance(value, str) and field in FIELDS:
                for old, new in mapping.items():
                    if value == old or value.startswith(old + "\\"):
                        target = (new / value[len(old):].lstrip("\\")).resolve()
                        if not target.is_relative_to(ROOT) or not target.exists():
                            unresolved.append({"file": str(path.relative_to(ROOT)), "field": field, "old": value})
                            return value
                        updated = str(target)
                        if updated != value:
                            changes.append({"field": field, "old": value, "new": updated})
                        return updated
            return value

        migrated = migrate(data)
        if changes:
            prepared.append((path, migrated, {"path": path.relative_to(ROOT).as_posix(), "before_sha256": sha(path), "changes": changes}))
    if unresolved:
        raise ValueError("存在无法验证的活动路径：" + json.dumps(unresolved, ensure_ascii=False))
    report = {"status": "migrated" if args.apply else "planned", "files": [v[2] for v in prepared],
              "changed_files": len(prepared), "changed_fields": sum(len(v[2]["changes"]) for v in prepared),
              "scope": "仅活动记录中的明确路径字段；原始模型响应、质量升级历史及原图片字节保持不变"}
    if args.apply:
        for path, data, record in prepared:
            if sha(path) != record["before_sha256"]:
                raise ValueError("迁移前记录变化，停止")
        for path, data, record in prepared:
            temporary = path.with_suffix(".migrating.tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
            record["after_sha256"] = sha(path)
        (ROOT / "migration_backup/path_migration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
