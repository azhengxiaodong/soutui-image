"""按用户明确要求重开测试：清空本项目运行记录和程序登记的输出，保留原图与登录配置。"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.runtime_lock import pipeline_lock


def safe(root, path):
    """删除前检查绝对目标及所有子项，不接受链接、项目根目录或越界路径。"""
    root, path = root.resolve(), Path(path)
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("重置目标越出项目")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("重置目标不能是链接")
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                raise ValueError("重置目录包含链接")
    return resolved


def reset(root, inputs, *, apply=False):
    """只删除已登记的组图和运行状态；未知用户文件触发停止，不清浏览器或网站远端记录。"""
    root = Path(root).resolve()
    targets = []
    originals = {}
    for directory in inputs:
        directory = safe(root, directory)
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        for manifest in directory.glob("*/.搜推生图.json"):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("app") != "soutui-image" or data.get("product_id") != manifest.parent.name:
                raise ValueError("输出目录身份不符，未删除")
            allowed = {".搜推生图.json", ".soutui.lock"}
            for group in data["groups"]:
                group_name = f"组{group['row']:02}"
                if group["directory"] != group_name:
                    raise ValueError("组号和目录不符")
                allowed.update(group_name + "/" + item["filename"] for item in group["saved"])
            for path in manifest.parent.rglob("*"):
                if path.is_file() and path.relative_to(manifest.parent).as_posix() not in allowed:
                    raise ValueError("输出目录有未登记用户文件，未清空：" + str(path))
            targets.append(safe(root, manifest.parent))
    state = root / "data"
    state.mkdir(exist_ok=True)
    # local存放本机加密会话和发布签名；settings仅保存用户选择的原图目录。
    for path in state.iterdir():
        if path.name not in {"local", "settings.json", "pipeline.lock"}:
            targets.append(safe(root, path))
    records = []
    for target in targets:
        files = [p for p in target.rglob("*") if p.is_file()] if target.is_dir() else [target]
        records.append({"path": target.relative_to(root).as_posix(), "files": len(files),
                        "bytes": sum(p.stat().st_size for p in files)})
    report = {"status": "reset" if apply else "planned", "targets": records,
              "files": sum(v["files"] for v in records), "bytes": sum(v["bytes"] for v in records),
              "originals_sha256": {str(p.relative_to(root)): h for p, h in originals.items()},
              "remote_website_history_deleted": False, "credentials_preserved": True}
    if apply:
        with pipeline_lock(state):
            # 锁获取成功后重检所有最终目标，防止运行中的流程被清空。
            for target in targets:
                safe(root, target)
            for target in targets:
                shutil.rmtree(target) if target.is_dir() else target.unlink()
        for path, expected in originals.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("原图发生非预期变化")
        (root / "migration_backup").mkdir(exist_ok=True)
        (root / "migration_backup/reset_latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", help="需清空程序输出的原图目录；可重复指定")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = reset(ROOT, args.input or [ROOT / "商品图片", ROOT / "白底"], apply=args.apply)
    print(json.dumps({k: v for k, v in report.items() if k != "originals_sha256"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
