"""公开源码检查：只读检查Git暂存区范围、密钥格式及冻结字节，不联网或发布。"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {".gitignore", ".gitattributes", "AGENTS.md", "README.md", "pyproject.toml", "requirements-build.txt"}
SECRET_PATTERNS = {
    "github_token": rb"(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,})",
    "openai_key": rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}",
    "private_key": rb"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{20,}",
    "aws_access_key": rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "bearer_literal": rb"Bearer [A-Za-z0-9_.-]{25,}",
    "jwt_literal": rb"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",
}


def allowed_path(name: str) -> bool:
    """仅允许当前源码、文本说明和已指定公开验收摘要；业务资料禁止入库。"""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if name in ROOT_FILES or (len(path.parts) == 1 and path.suffix == ".py"):
        return True
    if path.parts[0] == "src":
        return path.suffix in {".py", ".json"} and "__pycache__" not in path.parts
    if path.parts[0] in {"tests", "tools"}:
        return path.suffix == ".py" and "__pycache__" not in path.parts
    if path.parts[0] == "docs":
        return path.suffix == ".md"
    return name == "validation/frozen_modules.json" or (
        len(path.parts) == 3 and path.parts[:2] == ("validation", "public_handoff")
        and path.suffix in {".json", ".md"}
    )


def staged_files() -> dict[str, tuple[str, bytes]]:
    """从Git对象读取实际待提交内容，避免仅扫描工作目录而漏掉旧暂存内容。"""
    raw = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT)
    records = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise ValueError("Git暂存区存在未解决冲突")
        records.append((name.decode("utf-8"), mode, oid))
    result = subprocess.run(["git", "cat-file", "--batch"], cwd=ROOT,
                            input="".join(oid + "\n" for _, _, oid in records).encode("ascii"),
                            capture_output=True, check=True)
    position = 0
    files = {}
    for name, mode, oid in records:
        end = result.stdout.index(b"\n", position)
        actual_oid, kind, size = result.stdout[position:end].split()
        if actual_oid.decode("ascii") != oid or kind != b"blob":
            raise ValueError("Git暂存对象不是预期文件")
        start = end + 1
        stop = start + int(size)
        files[name] = mode, result.stdout[start:stop]
        position = stop + 1
    return files


def main() -> int:
    """输出不含密钥值的检查结论，发现越界、链接、常见密钥或基线异常即失败。"""
    files = staged_files()
    problems = []
    if not files:
        problems.append({"reason": "暂存区没有项目文件"})
    for name, (mode, raw) in files.items():
        if not allowed_path(name) or mode not in {"100644", "100755"}:
            problems.append({"file": name, "reason": "文件不在公开源码范围或属于链接"})
        if len(raw) > 2 * 1024 * 1024:
            problems.append({"file": name, "reason": "源码文件超过2MiB，请人工确认"})
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append({"file": name, "reason": "不应出现二进制或非UTF-8文件"})
        for detector, pattern in SECRET_PATTERNS.items():
            for match in re.finditer(pattern, raw):
                problems.append({"file": name, "reason": detector, "line": raw[:match.start()].count(b"\n") + 1})
    frozen_name = "validation/frozen_modules.json"
    frozen_count = 0
    if frozen_name not in files:
        problems.append({"reason": "缺少冻结基线"})
    else:
        baseline = json.loads(files[frozen_name][1].decode("utf-8"))
        expected = {name: digest for module in baseline["modules"].values() for name, digest in module["files"].items()}
        frozen_count = len(expected)
        for name, digest in expected.items():
            if name not in files or hashlib.sha256(files[name][1]).hexdigest() != digest:
                problems.append({"file": name, "reason": "暂存内容缺失或不符合冻结SHA-256"})
    report = {"status": "passed" if not problems else "failed", "scope": "Git暂存区公开范围、常见密钥格式及冻结字节",
              "files": len(files), "bytes": sum(len(raw) for _, raw in files.values()),
              "frozen_files": frozen_count, "problems": problems}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
