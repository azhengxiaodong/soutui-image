"""重装迁移工具：准备可恢复材料、验签私钥备份、逐文件校验压缩包后清理冗余。

所有业务模块只读；不提交生图、不调用审核模型。清理严格限于已登记的项目内路径。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "migration_backup"
ARCHIVE = ROOT / "搜推生图-重装恢复包-20261030.zip"
PASSWORD = ROOT / "仅自己保管-发布签名恢复口令.txt"
VERIFY = ROOT / "备份验收结果.json"
CN = timezone(timedelta(hours=8))


def now():
    return datetime.now(CN).isoformat()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def frozen_files():
    """读取登记基线并实际逐字节验证，不更新旧基线。"""
    result = {}
    for module in read(ROOT / "validation/frozen_modules.json")["modules"].values():
        for name, expected in module["files"].items():
            assert digest(ROOT / name) == expected, "冻结文件有非预期变化：" + name
            result[name.replace("\\", "/")] = expected
    return result


def ensure_idle():
    """只探测业务锁，不因锁文件存在就认为正在运行。"""
    sys.path.insert(0, str(ROOT / "src"))
    from soutui_image.runtime_lock import pipeline_lock
    with pipeline_lock(ROOT / "data"):
        pass


def safe_target(path):
    """删除前核实解析后绝对路径仍在项目内，拒绝链接、根目录及冻结文件。"""
    target = Path(path)
    resolved = target.resolve()
    assert resolved != ROOT and resolved.is_relative_to(ROOT), "清理越出项目目录"
    assert not target.is_symlink() and not getattr(target, "is_junction", lambda: False)()
    for name in frozen_files():
        protected = ROOT / name
        assert resolved != protected and not protected.is_relative_to(resolved), "不能清理冻结源码"
    return resolved


def prepare():
    """整理输入、合格图快照和网站已有结果；导出可跨Windows用户恢复的加密签名私钥。"""
    STATE.mkdir(exist_ok=True)
    frozen = frozen_files()
    ensure_idle()
    sys.path.insert(0, str(ROOT / "src"))
    from soutui_image.web_session import _protect
    from soutui_image.web_transport import WebTransport
    from soutui_image.pipeline import PipelinePaths
    from soutui_image.stage_d_web_generation import _apply_task
    from soutui_image.review_images import download_result
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # 口令只写到独立本地文件，不进入主压缩包和日志；私钥永不以明文文件落盘。
    private = STATE / "private/update_signing_key.encrypted.pem"
    if not private.exists():
        assert not PASSWORD.exists(), "已有口令文件但加密备份缺失，需核对"
        raw = _protect((ROOT / "data/local/update_signing_key.dpapi").read_bytes(), decrypt=True)
        key = Ed25519PrivateKey.from_private_bytes(raw)
        public = base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        assert public == read(ROOT / "src/soutui_image/resources/update_source.json")["public_key"]
        password = secrets.token_urlsafe(36).encode("ascii")
        encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.BestAvailableEncryption(password))
        private.parent.mkdir(exist_ok=True)
        with private.open("xb") as handle:
            handle.write(encrypted)
        with PASSWORD.open("x", encoding="utf-8") as handle:
            handle.write(password.decode() + "\n")
    recovered = serialization.load_pem_private_key(private.read_bytes(), PASSWORD.read_text(encoding="utf-8").strip().encode())
    actual_public = base64.b64encode(recovered.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    assert actual_public == read(ROOT / "src/soutui_image/resources/update_source.json")["public_key"]
    signature = recovered.sign(b"soutui-reinstall-backup-verification")
    recovered.public_key().verify(signature, b"soutui-reinstall-backup-verification")

    distributions = sorted({(d.metadata["Name"], d.version) for d in importlib.metadata.distributions()})
    (STATE / "environment-requirements.txt").write_text(
        "\n".join(f"{name}=={version}" for name, version in distributions) + "\n", encoding="utf-8")
    write(STATE / "environment.json", {"python": sys.version, "base_prefix": sys.base_prefix,
                                      "recreate_venv_after_reinstall": True})

    products = []
    for upload in sorted((ROOT / "data/tasks").glob("product_*_upload.json")):
        receipt = read(upload)["receipt"]
        pid = receipt["product_id"]
        source = Path(receipt["source_path"])
        if not source.exists() or digest(source) != receipt["source_sha256"]:
            source = ROOT / f"validation/quality_v2/f_io_live/input/{pid}.jpg"
        assert source.exists() and digest(source) == receipt["source_sha256"], "缺少真实白底图：" + pid
        destination = STATE / "materials/白底图" / (pid + source.suffix.lower())
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        group_states = []
        for group in sorted((ROOT / "data/tasks" / pid).glob("group_*")):
            summary = read(group / "review_summary.json") if (group / "review_summary.json").exists() else {}
            rounds = [read(p) for p in sorted(group.glob("round_*.json"))]
            group_states.append({"row": int(group.name[-2:]), "approved": summary.get("approved_count", 0),
                                 "reviewed_rounds": summary.get("reviewed_rounds", []),
                                 "rounds": [{k: r.get(k) for k in ["round_index", "state", "task_id", "submit_attempted_at"]} for r in rounds]})
            for item in summary.get("approved_results", []):
                report = read(item["report_path"])
                matches = [o for o in report["outcomes"] if o["candidate"]["sha256"] == item["sha256"]]
                assert len(matches) == 1 and matches[0]["review"]["decision"] == "pass"
                candidate = Path(matches[0]["candidate"]["local_path"])
                assert digest(candidate) == item["sha256"]
                saved = STATE / "materials/合格图快照" / pid / f"组{int(group.name[-2:]):02d}" / candidate.name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, saved)
        products.append({"product_id": pid, "source_sha256": receipt["source_sha256"],
                         "source_backup": str(destination.relative_to(ROOT)), "groups": group_states,
                         "approved_total": sum(g["approved"] for g in group_states)})

    # 只读查询已经提交的任务，下载其已成功图片；本工具绝不POST生成，也不调用审图模型。
    transport = WebTransport(PipelinePaths(ROOT / "data").session)
    jobs = [(p, read(p)) for p in sorted((ROOT / "data/tasks").glob("*/group_*/round_*.json"))]
    needed = {job["task_id"] for _, job in jobs if job.get("task_id")}
    found = {}
    for page in range(20):
        response = transport.request_json("GET", "/api/web/tasks", query={
            "featureType": "model-dressing", "limit": 200, "offset": page * 200})
        assert response.get("success") is True
        found.update({t["id"]: t for t in response.get("tasks", []) if t.get("id") in needed})
        if needed <= set(found) or not response.get("hasMore"):
            break
    downloads = []
    snapshots = []
    for path, job in jobs:
        if job.get("task_id") in found:
            job = _apply_task(path, job, found[job["task_id"]])
        snapshots.append({k: job.get(k) for k in ["product_id", "row", "round_index", "task_id", "state", "successful_results", "failed_results"]})
        for result in job.get("results", []):
            if result.get("status") == "done":
                downloads.append((job["product_id"], result, ROOT / "data/review_cache" / job["product_id"] /
                                  f"group_{job['row']:02d}" / f"round_{job['round_index']:02d}"))
    completed = []
    errors = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(download_result, *args): args for args in downloads}
        for future in as_completed(futures):
            args = futures[future]
            try:
                candidate = future.result()
                completed.append({"product_id": candidate.product_id, "result_id": candidate.result_id,
                                  "local_path": candidate.local_path, "sha256": candidate.sha256})
            except Exception as error:
                errors.append({"result_id": args[1]["result_id"], "error": str(error)})
    write(STATE / "website_results.json", {"jobs": snapshots, "downloaded": completed, "errors": errors})
    assert not errors, "部分已有图片尚未下载完成，不能清理：" + str(len(errors))
    write(STATE / "current_progress.json", {"captured_at": now(), "products": products,
        "current_target_product": "1084307568457", "business_complete": False,
        "stop_reason": "第1组补生第1轮的后两张尚未完成最终审核；18:49模型返回额度不足。第2至9组已生成，尚未逐张审核。",
        "automation_deleted_for_reinstall": "automation-3", "new_generation_submissions": 0, "new_model_calls": 0,
        "downloaded_existing_results": len(completed), "portable_signing_key_verified": True,
        "windows_sessions_require_login_again": True, "frozen_files": len(frozen)})
    print(json.dumps({"status": "prepared", "products": [{"id": p["product_id"], "approved": p["approved_total"]} for p in products],
                      "existing_results_backed_up": len(completed), "portable_key_verified": True}, ensure_ascii=False))


def make_plan():
    """只选择重复程序安装树、旧包和可重建缓存；实际审核证据和旧源码基线保留。"""
    frozen_files()
    candidates = [ROOT / p for p in ["build", ".venv", ".browser-profile", "release/history"]]
    # 已由原F模块恢复到正式便携目录的临时整理副本不重复保留。
    materials_check = STATE / "portable_materials_validation.json"
    if materials_check.exists() and read(materials_check).get("status") == "verified":
        candidates.append(STATE / "materials")
    candidates += [p.parent for p in (ROOT / "validation").rglob("搜推生图.exe") if (p.parent / "_internal").is_dir()]
    candidates += [p for p in (ROOT / "validation").rglob("*.zip") if p.name == "program.zip"]
    for parent in [ROOT / "data", ROOT / "validation", ROOT / "src", ROOT / "tests", ROOT / "tools", ROOT]:
        if parent == ROOT:
            candidates += [ROOT / "__pycache__"]
        else:
            candidates += list(parent.rglob("__pycache__"))
    for parent in [ROOT / "data", ROOT / "validation"]:
        candidates += [p for p in parent.rglob("*") if p.is_file() and p.suffix.lower() in {".tmp", ".part"}]
    selected = []
    for target in sorted(set(p for p in candidates if p.exists()), key=lambda p: len(p.parts)):
        if any(target.is_relative_to(old) for old in selected):
            continue
        safe_target(target)
        selected.append(target)
    items = []
    for target in selected:
        files = [p for p in target.rglob("*") if p.is_file()] if target.is_dir() else [target]
        items.append({"path": str(target.relative_to(ROOT)), "directory": target.is_dir(),
                      "files": len(files), "bytes": sum(p.stat().st_size for p in files)})
    write(STATE / "cleanup_plan.json", {"root": str(ROOT), "created_at": now(), "targets": items,
                                       "total_bytes": sum(i["bytes"] for i in items)})
    print(json.dumps({"status": "cleanup_planned", "targets": len(items), "bytes": sum(i["bytes"] for i in items)}, ensure_ascii=False))


def excluded(path, cleanup):
    rel = path.relative_to(ROOT)
    return (path == ARCHIVE or path == PASSWORD or path == VERIFY or rel.parts[0] == ".git" or
            path.suffix.lower() == ".dpapi" or "__pycache__" in rel.parts or
            rel.as_posix().startswith("data/local/") or rel.as_posix() in {
                "migration_backup/backup_manifest.json", "migration_backup/cleanup_report.json"} or
            any(path == target or path.is_relative_to(target) for target in cleanup))


def pack():
    """将保留文件打包，内容清单逐文件绑定SHA-256；口令和本机登录凭据始终排除。"""
    frozen_files()
    ensure_idle()
    plan = read(STATE / "cleanup_plan.json")
    cleanup = [ROOT / item["path"] for item in plan["targets"]]
    files = []
    for base, dirs, names in os.walk(ROOT, followlinks=False):
        dirs[:] = [d for d in dirs if not excluded(Path(base) / d, cleanup) and not (Path(base) / d).is_symlink()]
        for name in names:
            path = Path(base) / name
            if not excluded(path, cleanup) and not path.is_symlink():
                files.append(path)
    entries = {p.relative_to(ROOT).as_posix(): {"sha256": digest(p), "bytes": p.stat().st_size} for p in sorted(files)}
    manifest = {"format": "soutui-personal-reinstall-backup/1", "created_at": now(), "project_directory": "搜推生图",
                "password_file_separate": PASSWORD.name, "files": entries,
                "sensitive_personal_backup_not_software_distribution": True}
    write(STATE / "backup_manifest.json", manifest)
    temporary = ARCHIVE.with_suffix(".zip.part")
    assert not temporary.exists(), "上次压缩临时文件需核对"
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in files + [STATE / "backup_manifest.json"]:
            archive.write(path, "搜推生图/" + path.relative_to(ROOT).as_posix())
    temporary.replace(ARCHIVE)
    verify()


def verify():
    """从实际ZIP逐文件读回并计算哈希；不以存在文件或只看ZIP大小代替恢复验证。"""
    with zipfile.ZipFile(ARCHIVE) as archive:
        manifest = json.loads(archive.read("搜推生图/migration_backup/backup_manifest.json"))
        expected = {"搜推生图/" + p for p in manifest["files"]} | {"搜推生图/migration_backup/backup_manifest.json"}
        assert set(archive.namelist()) == expected
        for rel, item in manifest["files"].items():
            h = hashlib.sha256()
            with archive.open("搜推生图/" + rel) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(block)
            assert h.hexdigest() == item["sha256"], "ZIP内容哈希不符：" + rel
        assert not any(name.endswith(".dpapi") or "/data/local/" in name or PASSWORD.name in name for name in archive.namelist())
    result = {"status": "verified", "verified_at": now(), "archive": str(ARCHIVE), "archive_sha256": digest(ARCHIVE),
              "archive_bytes": ARCHIVE.stat().st_size, "verified_files": len(manifest["files"]),
              "includes_originals_tasks_review_checkpoints_and_program": True,
              "password_file": PASSWORD.name, "includes_windows_login_sessions": False,
              "business_complete": False, "other_computer_verified": False}
    write(VERIFY, result)
    print(json.dumps(result, ensure_ascii=False))


def cleanup():
    """仅在备份完整校验通过后删除登记的可重建副本；不删除当前软件或业务材料。"""
    ensure_idle()
    verified = read(VERIFY)
    assert verified["status"] == "verified" and digest(ARCHIVE) == verified["archive_sha256"]
    plan = read(STATE / "cleanup_plan.json")
    assert Path(plan["root"]).resolve() == ROOT
    before = frozen_files()
    removed, errors = [], []
    for item in plan["targets"]:
        target = safe_target(ROOT / item["path"])
        try:
            if target.is_dir():
                # 防止递归遍历外部目录。项目内部所有链接也不作为清理目标的一部分。
                assert not any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in target.rglob("*"))
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            removed.append(item)
        except Exception as error:
            errors.append({"path": item["path"], "error": str(error)})
    assert frozen_files() == before
    result = {"status": "complete" if not errors else "partial", "completed_at": now(), "removed": removed,
              "errors": errors, "reclaimed_bytes": sum(i["bytes"] for i in removed),
              "frozen_files_unchanged": True, "archive_sha256": digest(ARCHIVE)}
    write(STATE / "cleanup_report.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "removed"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "plan", "pack", "verify", "cleanup"])
    arguments = parser.parse_args()
    {"prepare": prepare, "plan": make_plan, "pack": pack, "verify": verify, "cleanup": cleanup}[arguments.action]()
