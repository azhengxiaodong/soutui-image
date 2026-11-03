"""开发保护工具：只读核对已验收板块，禁止静默改写旧模块的文件基线。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def check_frozen(root: Path, manifest_path: Path | None = None) -> list[str]:
    """返回冻结文件的异常；文件缺失、目录越界或内容变化都视为未通过。"""
    root = root.resolve()
    manifest_path = manifest_path or root / "validation" / "frozen_modules.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("modules"), dict):
        raise ValueError("冻结记录格式无效")
    problems = []
    for name, module in data["modules"].items():
        if not module.get("files"):
            raise ValueError(f"板块 {name} 的冻结文件清单为空")
        for relative_path, expected_hash in module["files"].items():
            path = (root / relative_path).resolve()
            if not path.is_relative_to(root):
                problems.append(f"{name}：冻结路径越出项目目录")
            elif not path.is_file():
                problems.append(f"{name}：文件缺失：{relative_path}")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                problems.append(f"{name}：已验收文件发生变化：{relative_path}")
    return problems


def main() -> int:
    """本工具仅检查，不提供自动更新基线的功能。"""
    try:
        problems = check_frozen(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"冻结检查未通过：{error}")
        return 1
    for problem in problems:
        print(problem)
    if not problems:
        print("已验收板块的文件校验通过。")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
