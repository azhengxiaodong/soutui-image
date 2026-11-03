"""F 的规则升级撤回：登记过的旧输出转存本机待复核区，验证字节后移出合格目录。"""

import copy
import hashlib
import json
import os
from pathlib import Path

from .stage_a_inputs import assert_source_unchanged
from .rule_runtime import current_policy
from .stage_f_outputs import (MANIFEST_NAME, _destination, _direct_path, _group_mapping,
                              _output_lock, _save_manifest)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_child(path, parent):
    _direct_path(path, parent)
    return path


def _write(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def withdraw_legacy_outputs(product, plan, directory):
    """输入当前原图、九组计划和本机待复核根目录；可断点恢复，不改原图/任务/缓存。"""
    assert_source_unchanged(product)
    if plan.product_id != product.product_id or plan.source_sha256 != product.sha256:
        raise ValueError("撤回计划不属于当前原图")
    output = _destination(product)
    manifest_path = _safe_child(output / MANIFEST_NAME, output)
    if not manifest_path.exists():
        return {"product_id": product.product_id, "withdrawn": 0, "pending": 0}
    archive = Path(directory).resolve()
    archive.mkdir(parents=True, exist_ok=True)
    if archive == output or archive.is_relative_to(output):
        raise ValueError("待复核区不能放进合格商品目录")
    operation = hashlib.sha256((current_policy() + product.product_id + product.sha256).encode()).hexdigest()[:24]
    target = _safe_child(archive / operation, archive); target.mkdir(exist_ok=True)
    journal = _safe_child(target / "journal.json", target)
    with _output_lock(output):
        raw = manifest_path.read_bytes()
        record = json.loads(raw)
        expected = _group_mapping(plan)
        if (record.get("app") != "soutui-image" or record.get("product_id") != product.product_id
                or record.get("source_sha256") != product.sha256
                or [{k:g[k] for k in expected[0]} for g in record["groups"]] != expected):
            raise ValueError("旧保存清单与当前商品或九组不一致")
        if journal.exists():
            transaction = json.loads(journal.read_text(encoding="utf-8"))
            if transaction["product_id"] != product.product_id or transaction["source_sha256"] != product.sha256:
                raise ValueError("撤回事务身份不符")
            if transaction["status"] == "complete":
                if any(x.get("policy_version") != current_policy() for g in record["groups"] for x in g["saved"]):
                    raise ValueError("已完成撤回后又出现未登记旧输出，需核对历史")
                return {"product_id": product.product_id, "withdrawn": 0,
                        "pending": sum(x.get("review_status") == "pending" for x in record.get("withdrawn", [])),
                        "history_directory": str(target), "resumed": True}
            if hashlib.sha256(raw).hexdigest() not in (transaction["before_sha256"], transaction.get("after_sha256")):
                raise ValueError("撤回过程中保存清单被更改，未覆盖")
        else:
            after = copy.deepcopy(record)
            moves = []
            for group in after["groups"]:
                keep = []
                for item in group["saved"]:
                    if item.get("policy_version") == current_policy():
                        keep.append(item); continue
                    if Path(item["filename"]).name != item["filename"]:
                        raise ValueError("旧文件名越界")
                    source_dir = _safe_child(output / group["directory"], output)
                    source = _safe_child(source_dir / item["filename"], source_dir)
                    if not source.is_file() or digest(source) != item["sha256"]:
                        raise ValueError("旧输出缺失或字节改变，未撤回其他图片")
                    copy_dir = _safe_child(target / group["directory"], target)
                    destination = _safe_child(copy_dir / item["filename"], copy_dir)
                    temporary = _safe_child(copy_dir / (".soutui-" + item["sha256"] + ".part"), copy_dir)
                    if temporary.exists():
                        raise FileExistsError("准备撤回时已有未知临时文件，未覆盖")
                    moves.append({"source":str(source), "destination":str(destination), "temporary":str(temporary), "sha256":item["sha256"]})
                    after.setdefault("withdrawn", []).append({**item, "row":group["row"], "group_directory":group["directory"],
                        "retained_path":str(destination), "review_status":"pending", "reason":"旧审核规则需重新核对，尚未判为不合格"})
                group["saved"] = keep
            if not moves:
                return {"product_id": product.product_id, "withdrawn":0,
                        "pending":sum(x.get("review_status") == "pending" for x in record.get("withdrawn", []))}
            after["output_policy_version"] = current_policy()
            backup = _safe_child(target / "manifest_before.json", target)
            if backup.exists() and backup.read_bytes() != raw:
                raise ValueError("待复核区已有不同保存清单，未覆盖")
            if not backup.exists(): backup.write_bytes(raw)
            transaction = {"product_id":product.product_id,"source_sha256":product.sha256,"policy_version":current_policy(),
                "status":"prepared","before_sha256":hashlib.sha256(raw).hexdigest(),
                "after_sha256":hashlib.sha256(json.dumps(after,ensure_ascii=False,indent=2).replace("\n",os.linesep).encode()).hexdigest(),
                "after":after,"moves":moves}
            _write(journal, transaction)
        # 日志在移动前落盘。每份先独占复制、fsync和哈希验证，再移除登记过的原输出。
        for move in transaction["moves"]:
            source, destination = Path(move["source"]), Path(move["destination"])
            if (source.resolve().parent.parent != output.resolve() or not destination.resolve().is_relative_to(target.resolve())):
                raise ValueError("事务文件路径越界")
            _direct_path(source, source.parent)
            _direct_path(source.parent, output)
            _direct_path(destination, destination.parent)
            _direct_path(destination.parent, target)
            destination.parent.mkdir(exist_ok=True)
            if not destination.exists():
                if not source.is_file() or digest(source) != move["sha256"]:
                    raise ValueError("撤回源文件缺失或变动")
                temporary = _safe_child(Path(move["temporary"]), destination.parent)
                # 临时文件在本事务创建前确认不存在，断电留下的自有.part可以从验证后的源图重新复制。
                with temporary.open("wb") as handle:
                    handle.write(source.read_bytes()); handle.flush(); os.fsync(handle.fileno())
                if digest(temporary) != move["sha256"]:
                    raise ValueError("待复核临时副本校验失败")
                os.rename(temporary, destination)
            if digest(destination) != move["sha256"]:
                raise ValueError("待复核副本校验失败，保留原输出")
            if source.exists():
                if digest(source) != move["sha256"]:
                    raise ValueError("复制后原输出被更改，未移除")
                source.unlink()
        assert_source_unchanged(product)
        current_hash = digest(manifest_path)
        if current_hash not in (transaction["before_sha256"], transaction["after_sha256"]):
            raise ValueError("提交前保存清单改变，未覆盖")
        if current_hash == transaction["before_sha256"]:
            _save_manifest(manifest_path, transaction["after"])
        transaction.update(status="complete",after_sha256=digest(manifest_path))
        _write(journal,transaction)
        return {"product_id":product.product_id,"withdrawn":len(transaction["moves"]),
                "pending":sum(x.get("review_status") == "pending" for x in transaction["after"]["withdrawn"]),
                "history_directory":str(target),"original_unchanged":True}
