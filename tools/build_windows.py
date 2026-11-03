"""构建 Windows 文件夹版；只收集运行代码和依赖，不复制账号、素材或任务数据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import ssl
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    """输出 dist/搜推生图；源码、构建日志与成品清单分别保存。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="搜推生图")
    parser.add_argument("--output-root", type=Path, default=ROOT / "dist", help="可先构建到dist内候选目录，验证后替换当前版")
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "validation/package", help="本次构建独立证据目录")
    args = parser.parse_args()
    if (not args.name or args.name in (".", "..") or args.name.rstrip(". ") != args.name
            or Path(args.name).name != args.name or any(c in args.name for c in '/\\:')):
        raise ValueError("软件名不能包含路径")
    # PyInstaller 会替换旧成品目录，先验证最终绝对目标始终在本项目 dist/build 下。
    dist_root = args.output_root.resolve()
    if not dist_root.is_relative_to((ROOT / "dist").resolve()):
        raise ValueError("程序输出必须在本项目dist内")
    for parent in (dist_root, ROOT / "build" / "windows"):
        if (parent / args.name).resolve().parent != parent.resolve():
            raise ValueError("构建目标越出本项目目录")
    evidence = args.evidence_dir.resolve()
    if not evidence.is_relative_to((ROOT / "validation").resolve()):
        raise ValueError("构建证据须在项目validation目录内")
    evidence.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "--onedir", "--windowed",
               "--name", args.name, "--distpath", str(dist_root),
               "--workpath", str(ROOT / "build" / "windows"), "--specpath", str(ROOT / "build"),
               "--paths", str(ROOT / "src"), "--paths", str(ROOT),
               "--collect-all", "uiautomation", "--collect-submodules", "comtypes",
               "--hidden-import", "tools.connect_existing_browser", str(ROOT / "main.py")]
    command[-1:-1] = ["--add-data",str(ROOT/"src/soutui_image/resources")+";soutui_image/resources"]
    # 当前 Python 发行版的 ssl 依赖这一随库证书资源；照常保留 HTTPS 验证策略。
    ssl_resource = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
    if ssl_resource.is_file():
        command[-1:-1] = ["--add-data", str(ssl_resource) + ";."]
    # 不使用 --add-data 项目目录，避免把本机登录态、审核缓存及商品图片带入成品。
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    (evidence / "build.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    if result.returncode:
        print(f"打包失败，详见 {evidence / 'build.log'}")
        return result.returncode
    output = dist_root / args.name
    files = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size,
                          "sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest()})
    record = {"status": "built_not_yet_smoke_tested", "executable": str(output / f"{args.name}.exe"),
              "file_count": len(files), "total_bytes": sum(f["bytes"] for f in files), "files": files,
              "includes_user_session": False, "other_computer_verified": False}
    record["source_sha256"] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted([ROOT / "main.py", ROOT / "run_pipeline.py", *list((ROOT / "src").rglob("*.py"))])}
    (evidence / "build_manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in record.items() if k != "files"}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
