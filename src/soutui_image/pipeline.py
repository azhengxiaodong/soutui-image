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
    def __init__(self, paths: PipelinePaths, *, on_event=None, stop_event=None, review_workers=2, model=None):
        if review_workers not in (1, 2, 4):
            raise ValueError("并行审核数只支持 1、2 或 4")
        self.paths = paths
        self.on_event = on_event or (lambda event: None)
        self.stop = stop_event or threading.Event()
        self.review_workers = review_workers
        self.model = model or DEFAULT_REVIEW_MODEL
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

    def _review(self, product, job):
        """输入当前原图和一轮任务；逐张审完所有成功结果，输出与原组绑定的审核汇总。"""
        row, round_index = job["row"], job["round_index"]
        root = self.paths.cache / product.product_id / f"group_{row:02}" / f"round_{round_index:02}"
        successful = [r for r in job["results"] if r["status"] == "done"]
        self.emit("reviewing", "逐张核对生成图，拟通过图片再独立复核", product_id=product.product_id,
                  row=row, round_index=round_index, total=len(successful))
        outcomes = []
        def worker(result):
            self.check_pause()
            candidate = download_result(product.product_id, result, root)
            self.check_pause()
            for attempt in range(2):
                self.check_pause()
                try:
                    review = review_candidate(product, candidate, self.paths.reviews, model=self.model)
                    break
                except TimeoutError:
                    if attempt: raise
                    self.emit("review_retry", "本图 Codex 等待超时，保留原图及任务后重试一次", product_id=product.product_id,
                              row=row, round_index=round_index, result_id=candidate.result_id)
            return {"candidate": asdict(candidate), "reference_id": result["reference_id"],
                    "reference_index": result["reference_index"], "review": asdict(review)}
        with ThreadPoolExecutor(max_workers=self.review_workers) as executor:
            futures = [executor.submit(copy_context().run, worker, result) for result in successful]
            for future in as_completed(futures):
                outcome = future.result(); outcomes.append(outcome)
                self.emit("image_reviewed", "一张图片审核完成", product_id=product.product_id, row=row,
                          round_index=round_index, completed=len(outcomes), total=len(successful),
                          decision=outcome["review"]["decision"], summary=outcome["review"]["summary"])
        report = {"product_id": product.product_id, "source_sha256": product.sha256, "row": row,
                  "round_index": round_index, "task_id": job["task_id"], "folder_id": job["folder_id"],
                  "reference_ids": job["reference_ids"], "failed_generation_count": job["failed_results"],
                  "status": "reviewed", "all_successful_images_reviewed": True,
                  "outcomes": sorted(outcomes, key=lambda item: item["reference_index"]),
                  "pass_count": sum(o["review"]["decision"] == "pass" for o in outcomes),
                  "reject_count": sum(o["review"]["decision"] == "reject" for o in outcomes),
                  "review_count": sum(o["review"]["decision"] == "review" for o in outcomes),
                  "approved_output_written": False}
        root.mkdir(parents=True, exist_ok=True)
        path = root / "round_review.json"; temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return write_group_review_summary(product, self.paths.tasks, self.paths.cache, row)

    def _save_all(self, product, plan):
        """输入商品及固定九组计划；只把完整审核报告交给 F，返回合格保存和去重数量。"""
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(
            (self.paths.cache / product.product_id).glob("group_*/round_*/round_review.json"))]
        complete = [r for r in reports if r.get("all_successful_images_reviewed")]
        result = save_approved_reports(product, plan, complete, self.paths.cache, self.paths.tasks)
        self.emit("saved", "合格图片已按组保存", **result)
        return result

    def run_product(self, product, rows):
        """为整款商品固定规则快照，包含九组、全部补生和并行审核，不在任务中途换版。"""
        self.check_pause()
        bundle=self.rule_store.for_product(product,self.paths.tasks)
        with use_rules(bundle):
            self.emit("rules_pinned","当前商品规则已固定",product_id=product.product_id,policy_version=current_policy(),rules_sha256=bundle.sha256)
            return self._run_product_with_rules(product,rows)

    def _run_product_with_rules(self, product, rows):
        """处理一张原图的指定组并返回组状态；补生不换参考图，最多三轮，不足则暂停该组。"""
        self.check_pause()
        # G：先处理旧规则保存状态；完成前不提交任何新生成，原轮次记录始终保留。
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
        manifest_path = output / ".搜推生图.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (manifest.get("product_id") != product.product_id or manifest.get("source_sha256") != product.sha256
                    or [(g["row"], g["folder_id"], g["reference_ids"]) for g in manifest["groups"]]
                    != [(g.row, g.folder_id, [r.image_id for r in g.references]) for g in plan.assignments]):
                raise ValueError("已有图片目录与当前原图或参考组不符，未提交新生成")
        previous_reports = list((self.paths.cache / product.product_id).glob("group_*/round_*/round_review.json"))
        if previous_reports:
            self._save_all(product, plan)
        # 先提交各组初轮，网站并行生成；已有任务记录的组不会另开初轮。
        for group in selected:
            self.check_pause(); assert_source_unchanged(product)
            existing = list((self.paths.tasks / product.product_id / f"group_{group.row:02}").glob("round_*.json"))
            if not existing:
                path, job = prepare_job(product, upload, plan, group.row, self.paths.tasks, client)
                self.emit("submitting", "提交初次生成", product_id=product.product_id, row=group.row,
                          round_index=0, expected_results=5, quoted_credits=job["quoted_credits"])
                submit_job(path, client)
        outcomes = []
        for group in selected:
            for _ in range(12):
                self.check_pause(); assert_source_unchanged(product)
                decision = group_decision(self.paths.tasks, product, group)
                if decision.action in ("complete", "paused"):
                    self._save_all(product, plan)
                    self.emit("group_" + decision.action, decision.reason, product_id=product.product_id,
                              row=group.row, approved_count=decision.approved_count)
                    outcomes.append({"row": group.row, "state": decision.action, "approved_count": decision.approved_count})
                    break
                if decision.action == "generate":
                    path, job = prepare_job(product, upload, plan, group.row, self.paths.tasks, client)
                    self.emit("submitting", "沿用原组参考图补生", product_id=product.product_id, row=group.row,
                              round_index=job["round_index"], expected_results=5, quoted_credits=job["quoted_credits"])
                    job = submit_job(path, client)
                else:
                    paths = sorted((self.paths.tasks / product.product_id / f"group_{group.row:02}").glob("round_*.json"))
                    path = paths[-1]; job = json.loads(path.read_text(encoding="utf-8"))
                    if job["state"] == "prepared": job = submit_job(path, client)
                if job["state"] not in ("awaiting_audit", "generation_failed"):
                    job = self._wait(path, client, product.product_id, group.row)
                self._review(product, job)
                self._save_all(product, plan)
            else:
                raise RuntimeError("当前组编排次数异常，已停止，不能继续补生")
        result = {"product_id": product.product_id, "groups": outcomes,
                  "status": "complete" if all(r["state"] == "complete" for r in outcomes) else "needs_attention"}
        self.emit("product_finished", "当前商品所选组处理结束", **result)
        return result

    def recheck_saved(self, directory, *, product_ids=None, upgrade_rules=False):
        """只复核已有输出，不连接网站或生成新图；用于修正规则升级及真实联调。"""
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
                    result=recheck_saved_product(product,self.paths,model=self.model,workers=self.review_workers,stop_event=self.stop,
                        on_event=lambda e:self.emit(e.pop("kind"),e.pop("message"),**e))
                results.append(result)
                if result["status"]=="paused": return {"status":"paused","products":results,"new_generation_submissions":0}
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
