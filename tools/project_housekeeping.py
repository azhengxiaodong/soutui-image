"""项目清理：默认预览；仅清理已识别的旧版本、测试工作副本和可重建缓存。"""

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEDIA = {".jpg", ".jpeg", ".png", ".webp"}


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def checked(path):
    """解析绝对路径并拒绝项目根目录、目录链接及任何越界目标。"""
    path = Path(path)
    resolved = path.resolve()
    if resolved == ROOT or not resolved.is_relative_to(ROOT):
        raise ValueError(f"清理目标越界：{path}")
    for part in (path, *path.parents):
        if part == ROOT:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError(f"不清理链接：{part}")
    return resolved


def make_plan():
    """按内容哈希确认测试图片已有正式副本；保留任务、审核断点、素材及当前版本。"""
    live = {}
    for base in (ROOT / "data", ROOT / "商品图片"):
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in MEDIA:
                live.setdefault(digest(path), path.relative_to(ROOT).as_posix())
    targets = {}

    def add(path, reason):
        path = checked(path)
        if path.exists():
            targets[path] = reason

    for name in ("release/history", "validation/history", "validation/updates/U_H/history", "build"):
        add(ROOT / name, "旧版本程序、源码快照或已完成构建的中间文件")
    add(ROOT / ".maintenance-tmp", "本轮依赖安装及验证的临时目录")
    for path in (ROOT / "src").glob("*.egg-info"):
        add(path, "可重建的源码打包元数据；当前虚拟环境的安装信息保留")
    add(ROOT / "release/旧版审核提醒.txt", "已由当前说明替代的旧提醒")
    validation = ROOT / "validation"
    # 只删除历史验收工作区；最终 result、构建清单、截图和独有正反例继续保留。
    for path in sorted(validation.rglob("*")):
        if path.is_relative_to(validation / "current_quality"):
            continue  # 当前人工标注样本、原始响应及验收证据仍在使用，不能按历史测试副本删除。
        if path.is_dir() and path.name in {"data", "input", "holding"}:
            pictures = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA]
            if pictures and all(digest(p) in live for p in pictures):
                add(path, "历史测试工作副本；全部图片已有哈希相同的正式文件")
        elif path.is_file() and path.suffix.lower() in MEDIA and digest(path) in live:
            add(path, "重复验收图片；正式数据中已有相同字节")
        elif path.is_file() and path.suffix == ".log":
            add(path, "已完成的历史构建或测试日志；验收结果与清单保留")
    for base in (ROOT, ROOT / "src", ROOT / "tools", ROOT / "tests"):
        if base == ROOT:
            candidates = [ROOT / "__pycache__", ROOT / ".pytest_cache", ROOT / ".ruff_cache"]
        else:
            candidates = list(base.rglob("__pycache__"))
        for path in candidates:
            add(path, "Python 可重建缓存")
    # 同一个目录和子文件只登记一次，删除前核验每个文件的字节与哈希。
    roots = [p for p in targets if not any(parent in targets for parent in p.parents)]
    entries = []
    for target in sorted(roots):
        files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
        records = []
        for path in files:
            checked(path)
            sha = digest(path)
            record = {"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": sha}
            if sha in live:
                record["retained_copy"] = live[sha]
            records.append(record)
        entries.append({"path": target.relative_to(ROOT).as_posix(), "reason": targets[target], "files": records})
    frozen = json.loads((ROOT / "validation/frozen_modules.json").read_text(encoding="utf-8"))
    protected = {p.replace("\\", "/") for module in frozen["modules"].values() for p in module["files"]}
    if any(item["path"] in protected for entry in entries for item in entry["files"]):
        raise ValueError("清单包含当前冻结文件")
    return {"root": str(ROOT), "created_at": datetime.now().astimezone().isoformat(),
            "status": "planned", "entries": entries,
            "file_count": sum(len(e["files"]) for e in entries),
            "bytes": sum(f["bytes"] for e in entries for f in e["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际执行；默认只预览")
    args = parser.parse_args()
    plan = make_plan()
    output = ROOT / "migration_backup" / ("cleanup_latest.json" if args.apply else "cleanup_preview.json")
    previous = json.loads(output.read_text(encoding="utf-8")) if args.apply and output.exists() else None
    if not plan["entries"]:
        preview = ROOT / "migration_backup/cleanup_preview.json"
        if preview.exists():
            preview.unlink()
        print(json.dumps({"status": "no_changes", "file_count": 0, "bytes": 0}))
        return
    # 公开克隆不携带本机维护目录；先安全创建日志目录，预览和实际清理均可首次运行。
    checked(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.apply:
        # 全部预检后再开始删除，数据文件和当前程序目录从不进入清理目标。
        for entry in plan["entries"]:
            for item in entry["files"]:
                path = checked(ROOT / item["path"])
                if digest(path) != item["sha256"]:
                    raise ValueError(f"清理前文件发生变化：{path}")
                if "retained_copy" in item and digest(ROOT / item["retained_copy"]) != item["sha256"]:
                    raise ValueError("正式副本不再完整，停止清理")
            target = checked(ROOT / entry["path"])
            if target.is_dir():
                for child in target.rglob("*"):
                    checked(child)
        for entry in plan["entries"]:
            target = checked(ROOT / entry["path"])
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        empty = []
        for path in sorted((ROOT / "validation").rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                checked(path).rmdir()
                empty.append(path.relative_to(ROOT).as_posix())
        plan["empty_directories_removed"] = empty
        plan["status"] = "deleted"
        if previous and previous.get("status") == "deleted" and previous.get("root") == str(ROOT):
            plan["this_run_file_count"], plan["this_run_bytes"] = plan["file_count"], plan["bytes"]
            plan["entries"] = previous["entries"] + plan["entries"]
            plan["file_count"] += previous["file_count"]
            plan["bytes"] += previous["bytes"]
        output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        preview = ROOT / "migration_backup/cleanup_preview.json"
        if preview.exists():
            preview.unlink()
    print(json.dumps({k: v for k, v in plan.items() if k != "entries"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
