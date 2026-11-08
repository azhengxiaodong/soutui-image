"""G 的审核规则升级：先保留旧图、逐张重审旧通过、留存旧报告，再重建计数，不提交生图。"""

import hashlib
import json
import shutil
from pathlib import Path

from .review_revision import plan_revision, execute_revision
from .review_group_state import write_group_review_summary
from .stage_a_inputs import assert_source_unchanged
from .stage_c_web_plan import load_plan
from .rule_runtime import current_policy
from .review_profiles import current_profile, profile_fingerprint, phase_profile_identity, validate_review_profile
from .stage_f_outputs import _qualified_items, save_approved_reports
from .stage_f_output_revision import withdraw_legacy_outputs
from .output_store import read_manifest


DEFAULT_REVIEW_MODEL = "gpt-6.1-sol"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def needs_upgrade(product, paths):
    """只读检查旧通过、旧汇总或待复核保存状态；旧拒绝保留原版本但不反复触发升级。"""
    output=Path(product.source_path).parent/product.product_id
    record=read_manifest(output,paths.tasks,product)
    if record is not None:
        if any(x.get("policy_version")!=current_policy() and x.get("review_status","pass") in ("pass","review")
               for g in record["groups"] for x in g["saved"]): return True
        if any(x.get("review_status")=="pending" for x in record.get("withdrawn",[])): return True
    for path in (paths.cache/product.product_id).glob("group_*/round_*/round_review.json"):
        report=_read(path)
        if report.get("quality_revision",{}).get("policy_version")==current_policy(): continue
        if any(o["review"]["policy_version"]!=current_policy() for o in report["outcomes"]): return True
    for path in (paths.tasks/product.product_id).glob("group_*/review_summary.json"):
        if _read(path).get("policy_version")!=current_policy(): return True
    return False


def recheck_saved_product(product, paths, *, model=DEFAULT_REVIEW_MODEL, workers=2, stop_event=None, on_event=None):
    """当前商品升级入口；只处理已有结果，所有生成任务和原三轮上限保持不变。"""
    emit=on_event or (lambda event:None)
    chosen = current_profile(model)
    assert_source_unchanged(product)
    if not needs_upgrade(product,paths): return {"product_id":product.product_id,"status":"already_current","new_generation_submissions":0}
    plan=load_plan(paths.tasks/f"product_{product.product_id}_references.json")
    identity = current_policy()+product.product_id+product.sha256+model
    if not chosen.legacy: identity += profile_fingerprint(chosen)
    operation=hashlib.sha256(identity.encode()).hexdigest()[:24]
    root=paths.root/"quality_revisions"/operation; root.mkdir(parents=True,exist_ok=True)
    withdrawn=withdraw_legacy_outputs(product,plan,paths.root/"quality_withdrawn",state_directory=paths.tasks)
    emit({"kind":"quality_withdrawn","message":"旧图已保留到本机待复核区，不计为新版合格",**withdrawn})
    staged_path=root/"result.json"
    staged=_read(staged_path) if staged_path.exists() else None
    if not staged or staged.get("status")!="staged":
        revision_plan=plan_revision([product],paths.tasks,paths.cache)
        emit({"kind":"quality_reviewing","message":"按新版规则逐张复核旧通过图片","product_id":product.product_id,
              "total":revision_plan["candidate_count"],"model":model})
        def progress(event):
            emit({"kind":"quality_image_reviewed","message":"一张旧图复核完成",**event})
        staged=execute_revision([product],revision_plan,root,paths.reviews,workers=workers,model=model,on_event=progress,stop_event=stop_event)
        if staged["status"]!="staged":
            if stop_event is not None and stop_event.is_set():
                return {"product_id":product.product_id,"status":"paused","new_generation_submissions":0}
            error=next((x.get("error") for x in staged.get("errors",[]) if x.get("status")=="error"),"审核未完成")
            raise RuntimeError("旧图重审尚未完成，已保留进度，未生新图；"+str(error))
    if staged["policy_version"]!=current_policy() or staged["model"]!=model:
        raise ValueError("复核阶段结果属于其他规则或模型")
    validate_review_profile(staged, model=model)
    revision_plan=_read(root/"plan.json")
    # 全部预检通过后才改正式报告；原任务文件只读，任何变动都先停止。
    for item in revision_plan["inputs"]:
        if not item.get("output_manifest") and _sha(item["job_path"])!=item["job_sha256"]:
            raise ValueError("原生成轮次已改变，未提交审核升级")
    reports=[]
    prepared=[]
    for item in staged["reports"]:
        source=Path(item["path"]).resolve(); destination=Path(item["old_path"]).resolve()
        if not source.is_relative_to((root/"reports").resolve()) or not destination.is_relative_to((paths.cache/product.product_id).resolve()):
            raise ValueError("审核升级报告路径越界")
        if source.name!="round_review.json" or destination.name!="round_review.json": raise ValueError("升级目标不是本程序轮次报告")
        if _sha(source)!=item["sha256"] or _sha(destination) not in (item["old_sha256"],item["sha256"]):
            raise ValueError("审核报告在升级过程中改变，未覆盖")
        report=_read(source); reports.append(report)
        prepared.append((item,source,destination))
    _qualified_items(product,plan,reports,paths.cache,paths.tasks)
    for item,source,destination in prepared:
        relative=destination.relative_to(paths.cache.resolve())
        backup=root/"history"/relative; backup.parent.mkdir(parents=True,exist_ok=True)
        if _sha(destination)==item["old_sha256"]:
            if backup.exists() and _sha(backup)!=item["old_sha256"]: raise ValueError("旧报告历史冲突")
            if not backup.exists(): shutil.copyfile(destination,backup)
            temporary=destination.with_suffix(".quality.tmp")
            temporary.write_bytes(source.read_bytes()); temporary.replace(destination)
        if not backup.exists() or _sha(backup)!=item["old_sha256"]:
            raise ValueError("缺少原审核报告历史，不能声明升级完成")
    saved=save_approved_reports(product,plan,reports,paths.cache,paths.tasks)
    summaries=[]
    for group in plan.assignments:
        if list((paths.tasks/product.product_id/f"group_{group.row:02}").glob("round_*.json")):
            summaries.append(write_group_review_summary(product,paths.tasks,paths.cache,group.row,allow_policy_revision=True))
    assert_source_unchanged(product)
    result={"product_id":product.product_id,"status":"rechecked","policy_version":current_policy(),"model":model,
            **phase_profile_identity(model=model),
            "reviewed_old_passes":staged["total"],"saved":saved,"group_counts":{s["row"]:s["approved_count"] for s in summaries},
            "original_rounds_preserved":True,"new_generation_submissions":0}
    (root/"apply_result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    emit({"kind":"quality_rechecked","message":"旧图复核与计数升级完成",**result})
    return result
