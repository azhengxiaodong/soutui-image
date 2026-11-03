"""板块 B：复用已有生图网页，创建模特换衣项目、上传原图、分类并以商品 ID 命名。

输入为板块 A 的一张商品记录；输出为已建成的一行项目与操作证据。
本板块不添加后续八行、不选择参考图、不提交生成，不消耗生图积分。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .browser_ui import ExistingBrowser, normalized
from .codex_clothing import ClothingClassification, classify_clothing
from .stage_a_inputs import ProductInput, assert_source_unchanged


def has(rows, name, role=None, *, prefix=False):
    """只匹配当前可见控件；相同文本在按钮和子文本上出现不会当成两次动作。"""
    return any(not c["offscreen"] and (role is None or c["type"] == role) and
               (normalized(c["name"]).startswith(normalized(name)) if prefix
                else normalized(c["name"]) == normalized(name)) for c in rows)


def verify_one_row(rows, product_id, category):
    """项目 ID、单行、服装类型及未生成状态必须同时成立。"""
    return (has(rows, f"模特换衣｜{product_id}")
            and sum(c["name"] == "重命名服装" and c["type"] == "ButtonControl"
                    and not c["offscreen"] for c in rows) == 1
            and has(rows, category, "TextControl")
            and has(rows, "未生成", "TextControl")
            and has(rows, "批量生成", "ButtonControl"))


@dataclass(frozen=True)
class ProjectReceipt:
    """后续板块按商品名定位项目，同时使用原图哈希防止串图。"""

    product_id: str
    project_name: str
    source_path: str
    source_sha256: str
    category: str
    clothing_rows: int
    status: str
    evidence_directory: str


class ProjectBuilder:
    """一次创建一个项目；每个网站动作记录前置与结果，异常不自动重试创建。"""

    def __init__(self, browser=None):
        self.browser = browser or ExistingBrowser()
        self.events = []
        self.journal_path = None
        self.context = {}

    def _save(self, status, **extra):
        report = {**self.context, "status": status, "events": self.events, **extra}
        temporary = self.journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.journal_path)

    def _click_wait(self, name, predicate, description, **options):
        # 先记待执行动作；进程中断时保留未知状态，禁止整段盲目重跑。
        self._save("running", pending_action=name)
        self.browser.click_name(name, **options)
        rows = self.browser.wait_for(predicate, description)
        self.events.append({"action": name, "result": rows})
        self._save("running")
        return rows

    def create(self, product: ProductInput, classification: ClothingClassification,
               state_directory: str | Path) -> ProjectReceipt:
        """新建一行项目。旧运行记录或同名项目存在时停止，保留人工核对入口。"""
        assert_source_unchanged(product)
        if (classification.product_id != product.product_id or classification.source_sha256 != product.sha256
                or classification.category not in {"套装", "上装", "下装"}):
            raise ValueError("服装分类与当前原图不对应，未操作网站")
        root = Path(state_directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.journal_path = root / f"project_{product.product_id}.json"
        if self.journal_path.exists():
            raise FileExistsError(f"当前商品有旧操作记录，停止重复创建；请使用 --verify-current 核验：{self.journal_path}")
        self.context = {"schema_version": 1, "product": asdict(product),
                        "classification": asdict(classification), "started_at": time.time()}
        self.events = []
        # 排他创建，确保两个程序不能同时为一个商品建立项目。
        with self.journal_path.open("x", encoding="utf-8") as handle:
            json.dump({**self.context, "status": "starting", "events": []}, handle, ensure_ascii=False)
        try:
            b = self.browser
            b.select_target_tab()
            rows = b.observe()
            if has(rows, "我有参考图", "ButtonControl", prefix=True):
                rows = self._click_wait("我有参考图", lambda r: has(r, "批量出图", "HyperlinkControl"),
                                        "模特换衣入口", role="ButtonControl", prefix=True)
            # 网站在同一路由内展示项目明细；侧边栏点同一路由不会退出明细，须先返回。
            if has(rows, "重命名项目", "ButtonControl") and has(rows, "返回", "ButtonControl"):
                rows = self._click_wait("返回", lambda r: has(r, "新建项目", "ButtonControl"),
                                        "退出当前项目明细", role="ButtonControl")
            # 首页及已建项目均经现有侧边栏进入项目列表，不打开新网页。
            self._click_wait("批量出图", lambda r: has(r, "新建项目", "ButtonControl"),
                             "批量项目列表", role="HyperlinkControl")
            rows = b.observe()
            if has(rows, product.product_id) or has(rows, f"模特换衣｜{product.product_id}"):
                raise FileExistsError("网站已有同名商品项目，停止重复创建，请核对现有项目")
            self._click_wait("新建项目", lambda r: has(r, "模特换衣 批量", "ButtonControl", prefix=True),
                             "项目功能选择", role="ButtonControl")
            self._click_wait("模特换衣 批量", lambda r: has(r, "确 定", "ButtonControl"),
                             "模特换衣选择", role="ButtonControl", prefix=True)
            self._click_wait("确 定", lambda r: has(r, "请选择服装类型", "ButtonControl")
                             and has(r, "选择文件: 未选择文件", "ButtonControl"),
                             "服装上传界面", role="ButtonControl")
            assert_source_unchanged(product)
            self._save("running", pending_action="选择原图")
            b.click_name("选择文件: 未选择文件", role="ButtonControl")
            b.wait_file_dialog()
            b.set_file_dialog_path(product.source_path)
            b.confirm_file_dialog()
            rows = b.wait_for(lambda r: has(r, f"选择文件: {Path(product.source_path).name}", "ButtonControl"),
                              "当前商品原图上传", timeout=60)
            self.events.append({"action": "上传当前原图", "file": product.source_path, "result": rows})
            self._save("running")
            self._click_wait("请选择服装类型", lambda r: has(r, classification.category, "ButtonControl"),
                             "服装类型菜单", role="ButtonControl")
            self._click_wait(classification.category, lambda r: has(r, classification.category, "ButtonControl")
                             and not has(r, "请选择服装类型", "ButtonControl"),
                             "服装类型选中", role="ButtonControl")
            self._click_wait("确 认", lambda r: has(r, "重命名项目", "ButtonControl")
                             and has(r, "未生成", "TextControl"), "已上传的一行项目", role="ButtonControl")
            self._save("running", pending_action="按商品 ID 重命名项目")
            rows = b.rename_project(product.product_id)
            self.events.append({"action": "重命名项目", "result": rows})
            if not verify_one_row(rows, product.product_id, classification.category):
                raise RuntimeError("项目建立后的商品名、行数或类型验证未通过")
            assert_source_unchanged(product)
            receipt = ProjectReceipt(product.product_id, product.product_id, product.source_path, product.sha256,
                                     classification.category, 1, "ready", str(root))
            self._save("ready", receipt=asdict(receipt))
            return receipt
        except Exception as error:
            try:
                current = self.browser.observe()
            except Exception:
                current = []
            self._save("needs_attention", error=str(error), observed_state=current)
            raise


def verify_current(product: ProductInput, state_directory: str | Path, *, browser=None):
    """只读复核已有成功记录对应的当前项目；不会再上传、改名或创建项目。"""
    root = Path(state_directory).resolve()
    report = json.loads((root / f"project_{product.product_id}.json").read_text(encoding="utf-8"))
    if report["status"] != "ready" or report["product"]["sha256"] != product.sha256:
        raise ValueError("尚无与当前原图对应的成功项目记录，需要核对之前的操作证据")
    assert_source_unchanged(product)
    b = browser or ExistingBrowser()
    b.select_target_tab()
    rows = b.observe()
    receipt = ProjectReceipt(**report["receipt"])
    if not verify_one_row(rows, product.product_id, receipt.category):
        raise RuntimeError("当前浏览器没有显示记录中的一行项目；请先在原浏览器选择该项目")
    return receipt


def run_stage_b(product: ProductInput, state_directory: str | Path, *, codex_exe=None):
    """独立业务入口：先完成图像分类，再进入网站创建，任何一步失败即停止该商品。"""
    root = Path(state_directory).resolve()
    if (root / f"project_{product.product_id}.json").exists():
        raise FileExistsError("当前商品已有操作记录；请核验现有项目，避免重复创建")
    classification = classify_clothing(product, root, codex_exe=codex_exe)
    return ProjectBuilder().create(product, classification, root)
