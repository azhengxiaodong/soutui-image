"""第一阶段统一编排：只调用已独立验收的 A～F，管理进度、暂停及断点续跑。

上传和收藏通过网站接口完成；每个商品九组，按组生成、逐图审核、保存并有上限补生。
不涉及店铺上传。模块内部规则保持冻结，编排只负责数据交接和运行顺序。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import asdict, dataclass
from pathlib import Path

from .atomic_json import atomic_write_json
from .codex_runtime import observe_calls
from .codex_clothing import ClothingClassification, classify_clothing
from .review_group_state import write_group_review_summary
from .review_images import download_result
from .stage_a_inputs import assert_source_unchanged, scan_inputs
from .stage_b_web_upload import upload_product
from .stage_c_web_plan import prepare_references
from .stage_d_web_generation import group_decision, prepare_job, query_job, submit_job
from .stage_e_codex_review import review_candidate
from .stage_f_outputs import save_approved_reports
from .web_session import MusefitReadClient
from .web_transport import WebTransport
from .quality_upgrade import DEFAULT_REVIEW_MODEL, recheck_saved_product
from .update_config import make_rule_store
from .rule_runtime import use_rules,current_policy
from .review_profiles import ReviewProfile, profile, use_review_profile, current_profile
from .workflow_options import bind_options
from .generation_settings import validate_resolution
from .output_store import read_manifest, output_lock, migrate_manifest


class UserPaused(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelinePaths:
    """输入本机数据根目录，统一给出会话、任务、审核缓存及证据位置。"""
    root: Path

    @property
    def session(self): return self.root / "local" / "musefit_session.dpapi"
    @property
    def tasks(self): return self.root / "tasks"
    @property
    def cache(self): return self.root / "review_cache"
    @property
    def reviews(self): return self.root / "reviews"


class Pipeline:
    """接收路径、进度回调和暂停信号；编排冻结板块，返回各商品与各组结果。"""
    def __init__(self, paths: PipelinePaths, *, on_event=None, stop_event=None, review_workers=4, model=None,
                 review_enabled=False, review_profile=None, resolution="2K"):
        if review_workers not in (1, 2, 4):
            raise ValueError("并行审核数只支持 1、2 或 4")
        self.paths = paths
        self.on_event = on_event or (lambda event: None)
        self.stop = stop_event or threading.Event()
        self.review_workers = review_workers
        self.requested_profile = review_profile or profile(model or DEFAULT_REVIEW_MODEL)
        if model is not None and model != self.requested_profile.model:
            raise ValueError("模型选择与审图配置不一致")
        self.model = self.requested_profile.model
        self.review_enabled = bool(review_enabled)
        self.requested_resolution = validate_resolution(resolution)
        self.active_options = None
        self.lock = threading.Lock()
        self.rule_store = make_rule_store(paths.root)

    def emit(self, kind, message, **data):
        event = {"time": time.time(), "kind": kind, "message": message, **data}
        with self.lock:
            self.on_event(event)

    def check_pause(self):
        if self.stop.is_set():
            raise UserPaused("已暂停新任务；已提交的生成任务保留在网站，继续运行会查回任务")

    def _upload(self, product):
        """输入当前原图身份；复用已绑定的分类/上传回执，输出可供各组共用的原图回执。"""
        path = self.paths.tasks / f"product_{product.product_id}_upload.json"
        self.emit("classifying", "核对服装类型和原图", product_id=product.product_id)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))["classification"]
            classification = ClothingClassification(**{**raw, "visible_garments": tuple(raw["visible_garments"])})
        else:
            classification = classify_clothing(product, self.paths.tasks / "classifications")
        self.check_pause()
        upload = upload_product(product, classification, self.paths.tasks, self.paths.session)
        self.emit("uploaded", "原图已上传并核对一致", product_id=product.product_id, category=upload.category)
        return upload

    def _wait(self, path, client, product_id, row):
        """按已记录任务号查询至完成；输出本轮结果，超时保留原任务而不重新提交。"""
        deadline = time.monotonic() + 2100
        previous = None
        while True:
            self.check_pause()
            record = query_job(path, client)
            state = (record["state"], record.get("successful_results"), record.get("failed_results"))
            if state != previous:
                self.emit("generation_progress", "查询生成进度", product_id=product_id, row=row,
                          state=record["state"], successful=record.get("successful_results", 0),
                          failed=record.get("failed_results", 0), round_index=record["round_index"])
                previous = state
            if record["state"] in ("awaiting_audit", "generation_failed"):
                return record
            if time.monotonic() >= deadline:
                raise TimeoutError("当前组生成跟踪超过 35 分钟，保留任务号，稍后继续查询")
            if self.stop.wait(8): self.check_pause()

    def _review_one(self, product, job, result):
        """单图沿用原下载、定位、完整审核和超时恢复；由调用方集中保存。"""
        row, round_index = job["row"], job["round_index"]
        root = self.paths.cache / product.product_id / f"group_{row:02}" / f"round_{round_index:02}"
        self.check_pause()
        candidate = download_result(product.product_id, result, root)
        self.check_pause()
        for attempt in range(2):
            self.check_pause()
            try:
                def progress(event):
                    state = {"started": "开始", "finished": "完成", "retry": "网络暂时中断，重试当前步骤",
                             "failed": "连接失败，保留断点", "timeout": "等待超时，保留断点"}[event["state"]]
                    elapsed = f"，耗时 {event['seconds']:.0f} 秒" if "seconds" in event else ""
                    self.emit("review_stage", f"第 {result['reference_index'] + 1} 张：{event['stage']}{state}{elapsed}",
                              product_id=product.product_id, row=row, round_index=round_index,
                              result_id=candidate.result_id, **event)
                with observe_calls(progress):
                    review = review_candidate(product, candidate, self.paths.reviews, model=self.model)
                break
            except TimeoutError:
                if attempt: raise
                self.emit("review_retry", "本图 Codex 等待超时，保留原图及任务后重试一次", product_id=product.product_id,
                          row=row, round_index=round_index, result_id=candidate.result_id)
        return {"candidate": asdict(candidate), "reference_id": result["reference_id"],
                "reference_index": result["reference_index"], "review": asdict(review)}

    def _review(self, product, job, *, on_progress=None):
        """输入当前原图和一轮任务；逐张审完所有成功结果，输出与原组绑定的审核汇总。"""
        row, round_index = job["row"], job["round_index"]
        root = self.paths.cache / product.product_id / f"group_{row:02}" / f"round_{round_index:02}"
        successful = [r for r in job["results"] if r["status"] == "done"]
        self.emit("reviewing", "逐张核对生成图，拟通过图片再独立复核", product_id=product.product_id,
                  row=row, round_index=round_index, total=len(successful))
        outcomes, errors = [], []
        with ThreadPoolExecutor(max_workers=self.review_workers) as executor:
            futures = [executor.submit(copy_context().run, self._review_one, product, job, result) for result in successful]
            for future in as_completed(futures):
                try:
                    outcome = future.result()
                except Exception as error:
                    errors.append(error)
                    self.emit("review_image_error", "本图审核中断；继续保存其他已完成的合格图", product_id=product.product_id,
                              row=row, round_index=round_index, error=str(error))
                    continue
                outcomes.append(outcome)
                self._write_round_report(product, job, outcomes, root, complete=False)
                self.emit("image_reviewed", "一张图片审核完成", product_id=product.product_id, row=row,
                          round_index=round_index, completed=len(outcomes), total=len(successful),
                          decision=outcome["review"]["decision"], summary=outcome["review"]["summary"])
                if on_progress:
                    on_progress()
        if errors:
            raise errors[0]
        self._write_round_report(product, job, outcomes, root, complete=True)
        return write_group_review_summary(product, self.paths.tasks, self.paths.cache, row)

    def _write_round_report(self, product, job, outcomes, root, *, complete):
        """逐图原子落盘；未审完报告与完整轮次分开，不能据此提前补生或判组完成。"""
        row, round_index = job["row"], job["round_index"]
        report = {"product_id": product.product_id, "source_sha256": product.sha256, "row": row,
                  "round_index": round_index, "task_id": job["task_id"], "folder_id": job["folder_id"],
                  "reference_ids": job["reference_ids"], "failed_generation_count": job["failed_results"],
                  "status": "reviewed" if complete else "reviewing", "all_successful_images_reviewed": complete,
                  "outcomes": sorted(outcomes, key=lambda item: item["reference_index"]),
                  "pass_count": sum(o["review"]["decision"] == "pass" for o in outcomes),
                  "reject_count": sum(o["review"]["decision"] == "reject" for o in outcomes),
                  "review_count": sum(o["review"]["decision"] == "review" for o in outcomes),
                  "approved_output_written": False}
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("round_review.json" if complete else "partial_review.json")
        atomic_write_json(path, report)
        if complete:
            (root / "partial_review.json").unlink(missing_ok=True)

    def _save_all(self, product, plan):
        """逐图保存已完成双审的结果；未审完的组仍须继续审核，不能触发提前补生。"""
        reports = []
        for directory in sorted((self.paths.cache / product.product_id).glob("group_*/round_*")):
            full, partial = directory / "round_review.json", directory / "partial_review.json"
            path = full if full.exists() else partial
            if path.exists():
                reports.append(json.loads(path.read_text(encoding="utf-8")))
        pending = bool(self.active_options and not self.active_options.get("legacy"))
        result = save_approved_reports(product, plan, reports, self.paths.cache, self.paths.tasks,
                                       allow_partial=True, include_pending=pending)
        self.emit("saved", "合格图片已按组保存", **result)
        return result

    def run_product(self, product, rows):
        """为整款商品固定规则快照，包含九组、全部补生和并行审核，不在任务中途换版。"""
        self.check_pause()
        options = bind_options(product, self.paths.tasks, review_enabled=self.review_enabled,
                               selected_profile=self.requested_profile, resolution=self.requested_resolution)
        chosen = ReviewProfile.from_dict(options["review_profile"])
        bundle=self.rule_store.for_product(product,self.paths.tasks)
        previous_model, previous_options = self.model, self.active_options
        self.model, self.active_options = chosen.model, options
        try:
            with use_rules(bundle), use_review_profile(chosen):
                self.emit("run_options", "沿用商品已固定的执行模式和模型", product_id=product.product_id,
                          review_enabled=options["review_enabled"], review_profile=chosen.to_dict(),
                          profile=chosen.to_dict(), legacy=options.get("legacy", False),
                          resolution=options["resolution"])
                self.emit("rules_pinned","当前商品规则已固定",product_id=product.product_id,policy_version=current_policy(),rules_sha256=bundle.sha256)
                return self._run_product_with_rules(product,rows)
        finally:
            self.model, self.active_options = previous_model, previous_options

    def _run_product_with_rules(self, product, rows):
        """处理一张原图的指定组并返回组状态；补生不换参考图，最多三轮，不足则暂停该组。"""
        self.check_pause()
        # G：先处理旧规则保存状态；完成前不提交任何新生成，原轮次记录始终保留。
        enabled = self.active_options is None or self.active_options["review_enabled"]
        if enabled:
            recheck_saved_product(product,self.paths,model=self.model,workers=self.review_workers,
                                 stop_event=self.stop,on_event=lambda e:self.emit(e.pop("kind"),e.pop("message"),**e))
        self.check_pause()
        client = WebTransport(self.paths.session)
        data = MusefitReadClient(self.paths.session).get_json("/api/web/favorites", {"mediaType": "image"})
        plan = prepare_references(product, data, self.paths.tasks)
        self.emit("references_ready", "九个不同参考组已固定，共四十五张参考图", product_id=product.product_id)
        upload = self._upload(product)
        selected = [g for g in plan.assignments if g.row in rows]
        output = Path(product.source_path).parent / product.product_id
        manifest = read_manifest(output, self.paths.tasks, product)
        if manifest is not None:
            if (manifest.get("product_id") != product.product_id or manifest.get("source_sha256") != product.sha256
                    or [(g["row"], g["folder_id"], g["reference_ids"]) for g in manifest["groups"]]
                    != [(g.row, g.folder_id, [r.image_id for r in g.references]) for g in plan.assignments]):
                raise ValueError("已有图片目录与当前原图或参考组不符，未提交新生成")
            with output_lock(output, self.paths.tasks):
                migrate_manifest(output, self.paths.tasks, product, plan)
        if not enabled:
            from .generation_only import run_generation_only
            outcomes = run_generation_only(self, product, plan, upload, selected, client)
            result = {"product_id": product.product_id, "groups": outcomes, "review_enabled": False,
                      "status": "complete" if all(r["state"] == "complete" for r in outcomes) else "needs_attention"}
            self.emit("product_finished", "生图与保存结束，图片未审核", **result)
            return result
        previous_reports = list((self.paths.cache / product.product_id).glob("group_*/round_*/*review.json"))
        if previous_reports:
            self._save_all(product, plan)
        # G：任意组的单张结果完成即入共享审核池；查询、审核和补生等待互不阻塞。
        from .streaming_pipeline import run_streaming_product
        outcomes = run_streaming_product(self, product, plan, upload, selected, client)
        result = {"product_id": product.product_id, "groups": outcomes,
                  "status": "complete" if all(r["state"] == "complete" for r in outcomes) else "needs_attention"}
        self.emit("product_finished", "当前商品所选组处理结束", **result)
        return result

    def regenerate(self, directory, *, product_ids=None):
        """手动对实际不足三张的原组各补一轮；永不审图，独立操作不重置原自动补生。"""
        from .manual_regeneration import run_regeneration
        batch = scan_inputs(directory)
        if not batch.ready: raise ValueError("白底图目录扫描不通过")
        selected = [p for p in batch.products if not product_ids or p.product_id in product_ids]
        if not selected or (product_ids and set(product_ids) - {p.product_id for p in selected}):
            raise ValueError("所选商品没有对应有效原图")
        results = []
        try:
            for product in selected:
                self.check_pause()
                bundle = self.rule_store.for_product(product, self.paths.tasks)
                with use_rules(bundle): results.append(run_regeneration(self, product))
        except UserPaused:
            return {"status": "paused", "products": results, "review_enabled": False}
        return {"status": "complete" if all(r["status"] == "complete" for r in results) else "needs_attention",
                "products": results, "review_enabled": False}

    def recheck_saved(self, directory, *, product_ids=None, upgrade_rules=False):
        """单独审核实际已保存图片，不要求网站连接，不提交生图或自动补生。"""
        from .saved_review import run_saved_review
        batch=scan_inputs(directory)
        if not batch.ready: raise ValueError("白底图目录扫描不通过")
        selected=[p for p in batch.products if not product_ids or p.product_id in product_ids]
        if not selected: raise ValueError("没有可复核的商品")
        results=[]
        try:
            for product in selected:
                self.check_pause()
                bundle=self.rule_store.for_product(product,self.paths.tasks,upgrade=upgrade_rules)
                with use_rules(bundle):
                    result=run_saved_review(self, product, self.requested_profile)
                results.append(result)
                if result["status"]=="paused": return {"status":"paused","products":results,"new_generation_submissions":0}
                if result["status"]=="needs_attention": return {"status":"needs_attention","products":results,"new_generation_submissions":0}
        except UserPaused:
            return {"status":"paused","products":results,"new_generation_submissions":0}
        return {"status":"complete","products":results,"new_generation_submissions":0}

    def run(self, directory, *, product_ids=None, rows=tuple(range(1, 10))):
        """输入白底图目录和可选商品/组号；顺序处理商品，返回完成、暂停或需处理的总结果。"""
        if not rows or len(set(rows)) != len(rows) or any(r not in range(1, 10) for r in rows):
            raise ValueError("处理组号必须在 1～9 且不重复")
        batch = scan_inputs(directory)
        if not batch.ready:
            raise ValueError("白底原图目录有异常，请先检查文件名、重复商品 ID 和损坏图片")
        selected = [p for p in batch.products if not product_ids or p.product_id in product_ids]
        if not selected or (product_ids and set(product_ids) - {p.product_id for p in selected}):
            raise ValueError("所选商品 ID 没有对应有效原图")
        results = []
        self.emit("started", "开始第一阶段任务", products=len(selected), groups_per_product=len(rows))
        try:
            for product in selected:
                results.append(self.run_product(product, rows))
        except UserPaused:
            self.emit("paused", "已暂停，保留原任务和已保存的合格图")
            return {"status": "paused", "products": results}
        result = {"status": "complete" if all(p["status"] == "complete" for p in results) else "needs_attention", "products": results}
        self.emit("finished", "本次处理结束", **result)
        return result
