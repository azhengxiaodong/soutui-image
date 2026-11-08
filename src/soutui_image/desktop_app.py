"""本地桌面工作台：用户选目录后明确开始，后台接口与模型工作通过事件更新界面。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .browser_connection import connect_existing_session
from .codex_clothing import find_codex
from .pipeline import Pipeline, PipelinePaths
from .quality_upgrade import DEFAULT_REVIEW_MODEL
from .stage_e_codex_review import POLICY_VERSION
from .runtime_lock import pipeline_lock
from .stage_a_inputs import scan_inputs
from .stage_c_web_plan import load_plan
from .stage_d_web_generation import group_decision, quote_balanced
from .web_session import MusefitReadClient
from .web_transport import WebTransport
from .update_service import UpdateService
from .update_dialog import show_access_dialog
from .update_config import APP_VERSION,display_bundle
from .rule_runtime import use_rules
from .rule_store import policy_id
from .atomic_json import atomic_write_json
from .output_store import read_manifest
from .saved_images import counts as output_counts
from .review_profiles import profile, SUPPORTED_MODELS
from .workflow_options import read_options


CN_TZ = timezone(timedelta(hours=8))
COLORS = {"background": "#f3f5f9", "panel": "#ffffff", "ink": "#18253c", "muted": "#66758b",
          "blue": "#2463eb", "green": "#16866d", "orange": "#b56b16", "red": "#bc3b45"}
SESSION_SETTING_KEYS = ("input_directory", "review_model", "resolution", "review_workers", "review_enabled")


class Workbench:
    def __init__(self, root, data_directory, initial_directory=None):
        self.root = root
        self.paths = PipelinePaths(Path(data_directory).resolve())
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.connected = False
        self.codex_ready = False
        self.installing_update = False
        self.closed = False
        self.connection_in_progress = False
        self.automatic_connection_attempted = False
        self.pending_start = False
        self.pending_regeneration = False
        self.settings_window = None
        self.pending_window = None
        self.active_options = {}
        self.output_errors = {}
        self.update_service = UpdateService(self.paths.root)
        self.update_text = tk.StringVar(master=root, value="正在检查私有规则更新…")
        self.current_product = None
        self.products = {}
        self.group_states = {}
        self.input_path = tk.StringVar(master=root)
        self.resolution = tk.StringVar(master=root, value="2K")
        self.resolution_button_text = tk.StringVar(master=root)
        self.review_enabled = tk.BooleanVar(master=root, value=False)
        self.review_model = tk.StringVar(master=root, value=DEFAULT_REVIEW_MODEL)
        self.model_button_text = tk.StringVar(master=root)
        self.subtitle_text = tk.StringVar(master=root)
        self.account_text = tk.StringVar(master=root, value="正在检查本机连接…")
        self.codex_text = tk.StringVar(master=root, value="正在检查 Codex…")
        self.status_text = tk.StringVar(master=root, value="选择白底图文件夹，检查完成后开始")
        self.quote_text = tk.StringVar(master=root, value="每商品首轮 45 张；每组不足 3 张时，原组最多补生 3 轮。")
        self.settings_data = {}
        self.price_quotes = {}
        self.quote_unavailable = {}
        settings = self.paths.root / "settings.json"
        if settings.exists():
            try:
                self.settings_data = json.loads(settings.read_text(encoding="utf-8"))
                if not isinstance(self.settings_data, dict): self.settings_data = {}
                # 每次打开从空白界面和默认选项开始；只清理旧界面偏好，任务与登录文件不动。
                if any(key in self.settings_data for key in SESSION_SETTING_KEYS): self._save_settings()
            except (OSError, ValueError): pass
        # 显式命令行 --input 是本次指定的输入，不属于恢复上次界面。
        if initial_directory: self.input_path.set(str(initial_directory))
        self._build()
        self._update_options_text()
        if self.input_path.get(): self.scan()
        self.root.after(100, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._background(self.check_environment)
        self._background(self.check_updates)
        self.root.after(30000, self._poll_connection)

    def _build(self):
        self.root.title("搜推生图 · 本地工作台")
        self.root.geometry("1140x920")
        self.root.minsize(980, 820)
        self.root.configure(bg=COLORS["background"])
        style = ttk.Style(self.root); style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=COLORS["background"], foreground=COLORS["ink"])
        style.configure("Card.TLabel", background="white")
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure("TButton", padding=(12, 7), background="white", borderwidth=1)
        style.configure("Primary.TButton", background=COLORS["blue"], foreground="white", padding=(18, 9))
        style.map("Primary.TButton", background=[("active", "#1c50c5"), ("disabled", "#aebbd4")])
        # 本排使用独立白底黑字样式，不画控件外框，也不改其他区域主题。
        style.configure("Options.TFrame", background="white")
        style.configure("Options.TCheckbutton", background="white", foreground="black", padding=0,
                        borderwidth=0, relief="flat", indicatorbackground="white", indicatorforeground="black")
        # 原生 clam 的选中符号是叉号；用24像素指示器绘制明确的对号，仍保留键盘/焦点行为。
        from PIL import Image, ImageDraw, ImageTk
        self.review_indicator_images = []
        for checked in (False, True):
            icon = Image.new("RGB", (26, 26), "white")
            draw = ImageDraw.Draw(icon); draw.rectangle((2, 2, 23, 23), outline="black", width=2)
            if checked: draw.line(((6, 13), (11, 18), (20, 7)), fill="black", width=3)
            self.review_indicator_images.append(ImageTk.PhotoImage(icon, master=self.root))
        style.element_create("ReviewCheck.indicator", "image", self.review_indicator_images[0],
                             ("selected", self.review_indicator_images[1]), sticky="")
        style.map("Options.TCheckbutton", background=[("active", "white"), ("disabled", "white")],
                  foreground=[("disabled", "#666666")], indicatorbackground=[("selected", "white"), ("!selected", "white")])
        style.layout("Options.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("ReviewCheck.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})])
        style.configure("Options.TMenubutton", background="white", foreground="black", padding=(2, 7),
                        arrowcolor="black", borderwidth=0, relief="flat")
        style.map("Options.TMenubutton", background=[("active", "white"), ("disabled", "white")],
                  foreground=[("disabled", "#666666")], arrowcolor=[("disabled", "#666666")])
        style.layout("Options.TMenubutton", [("Menubutton.padding", {"sticky": "nswe", "children": [
            ("Menubutton.indicator", {"side": "right", "sticky": ""}),
            ("Menubutton.label", {"side": "left", "sticky": ""})]})])
        style.configure("Treeview", rowheight=28, background="white", fieldbackground="white", foreground=COLORS["ink"])
        style.configure("Treeview.Heading", background="#eaf0fa", font=("Microsoft YaHei UI", 10, "bold"))
        main = ttk.Frame(self.root, padding=16); main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=3, minsize=280)
        main.rowconfigure(6, weight=2, minsize=140)
        card = ttk.Frame(main, style="Card.TFrame", padding=16); card.grid(row=0, column=0, sticky="ew")
        ttk.Label(card, text="白底图文件夹", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        self.path_entry = ttk.Entry(card, textvariable=self.input_path); self.path_entry.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        self.browse_button = ttk.Button(card, text="选择文件夹", command=self.browse)
        self.browse_button.grid(row=1, column=1, padx=(10, 0), pady=(9, 0))
        self.scan_button = ttk.Button(card, text="扫描目录", command=self.scan); self.scan_button.grid(row=1, column=2, padx=(8, 0), pady=(9, 0))
        card.columnconfigure(0, weight=1)
        panes = ttk.Panedwindow(main, orient="horizontal"); panes.grid(row=1, column=0, sticky="nsew", pady=(10, 10))
        left = ttk.Frame(panes, style="Card.TFrame", padding=12)
        right = ttk.Frame(panes, style="Card.TFrame", padding=12)
        panes.add(left, weight=2); panes.add(right, weight=3)
        ttk.Label(left, text="商品 · 可多选", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        products = ttk.Frame(left, style="Card.TFrame"); products.pack(fill="both", expand=True)
        self.product_tree = ttk.Treeview(products, columns=("id", "size", "saved", "pass", "review"), show="headings", selectmode="extended", height=9)
        for col, title, width in (("id", "商品 ID", 145), ("size", "尺寸", 80), ("saved", "保存", 50), ("pass", "通过", 50), ("review", "待确认", 60)):
            self.product_tree.heading(col, text=title); self.product_tree.column(col, width=width, anchor="center")
        self.product_scrollbar = ttk.Scrollbar(products, orient="vertical", command=self.product_tree.yview)
        self.product_tree.configure(yscrollcommand=self.product_scrollbar.set)
        self.product_scrollbar.pack(side="right", fill="y")
        self.product_tree.pack(side="left", fill="both", expand=True)
        self.product_tree.bind("<<TreeviewSelect>>", self.show_selected_product)
        ttk.Label(right, text="当前商品 · 九个参考组", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        groups = ttk.Frame(right, style="Card.TFrame"); groups.pack(fill="both", expand=True)
        self.group_tree = ttk.Treeview(groups, columns=("group", "name", "state", "saved", "pass", "review", "round"), show="headings", height=9)
        for col, title, width in (("group", "组号", 40), ("name", "参考分组", 100), ("state", "当前进度", 170),
                                  ("saved", "保存", 45), ("pass", "通过", 45), ("review", "待确认", 60), ("round", "补生", 45)):
            self.group_tree.heading(col, text=title); self.group_tree.column(col, width=width, anchor="center")
        self.group_scrollbar = ttk.Scrollbar(groups, orient="vertical", command=self.group_tree.yview)
        self.group_tree.configure(yscrollcommand=self.group_scrollbar.set)
        self.group_scrollbar.pack(side="right", fill="y")
        self.group_tree.pack(side="left", fill="both", expand=True)
        for i in range(1, 10): self.group_tree.insert("", "end", iid=str(i), values=(f"{i:02}", "待读取", "尚未开始", 0, 0, 0, 0))
        controls = ttk.Frame(main, style="Card.TFrame"); controls.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.start_button = ttk.Button(controls, text="开始", style="Primary.TButton", command=self.start); self.start_button.pack(side="left")
        self.review_check = ttk.Checkbutton(controls, text="开启审图", variable=self.review_enabled,
                                            style="Options.TCheckbutton", command=self._update_options_text)
        self.review_check.pack(side="left", padx=(14, 6))
        self.model_button = ttk.Combobox(controls, textvariable=self.model_button_text, state="readonly", width=16,
                                         values=tuple(self._model_label(model) for model in SUPPORTED_MODELS))
        self.model_button.bind("<<ComboboxSelected>>", self._model_selected)
        self.model_button.pack(side="left", padx=(0, 12))
        self.recheck_button = ttk.Button(controls, text="单独审图", command=self.recheck); self.recheck_button.pack(side="left")
        self.regenerate_button = ttk.Button(controls, text="重新生图", command=self.regenerate)
        self.regenerate_button.pack(side="left", padx=(9, 0))
        extras = ttk.Frame(main); extras.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        self.resolution_button = ttk.Menubutton(extras, textvariable=self.resolution_button_text, style="Options.TMenubutton")
        self.resolution_menu = tk.Menu(self.resolution_button, tearoff=False)
        for resolution in ("2K", "4K"):
            self.resolution_menu.add_radiobutton(label=resolution, value=resolution, variable=self.resolution,
                                                command=self._resolution_changed)
        self.resolution_button.configure(menu=self.resolution_menu); self.resolution_button.pack(side="left")
        self.pause_button = ttk.Button(extras, text="暂停新任务", command=self.pause, state="disabled"); self.pause_button.pack(side="left", padx=9)
        ttk.Button(extras, text="查看图片目录", command=self.open_output).pack(side="left")
        self.pending_button = ttk.Button(extras, text="待确认图片", command=self.show_pending_images)
        self.pending_button.pack(side="left", padx=(9, 0))
        self.quote_label = ttk.Label(main, textvariable=self.quote_text, style="Muted.TLabel", wraplength=1030)
        self.settings_button = ttk.Button(extras, text="设置", command=self.show_settings); self.settings_button.pack(side="right")
        self.quote_label.grid(row=4, column=0, sticky="ew", pady=(0, 5))
        main.bind("<Configure>", lambda event: self.quote_label.configure(wraplength=max(400, event.width - 48)))
        ttk.Label(main, textvariable=self.status_text, font=("Microsoft YaHei UI", 10, "bold"), wraplength=1030).grid(row=5, column=0, sticky="w", pady=(2, 5))
        progress = ttk.Frame(main, style="Card.TFrame"); progress.grid(row=6, column=0, sticky="nsew")
        self.log = tk.Text(progress, height=8, wrap="word", relief="flat", bg="white", fg=COLORS["muted"],
                           font=("Microsoft YaHei UI", 9), padx=10, pady=8, state="disabled")
        self.log_scrollbar = ttk.Scrollbar(progress, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=self.log_scrollbar.set)
        self.log_scrollbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    @staticmethod
    def _model_label(model):
        return {"gpt-6.1-sol": "GPT-6.1 Sol", "gpt-6.1-terra": "GPT-6.1 Terra"}.get(model, model)

    def _update_options_text(self):
        """新任务选项只更新显示；已有任务的实际配置由 run_options 事件说明。"""
        self.model_button_text.set(self._model_label(self.review_model.get()))
        self.resolution_button_text.set("出图尺寸：" + self.resolution.get())
        enabled = self.review_enabled.get()
        self.subtitle_text.set("白底图  →  九组生成与审图并行  →  保留通过及待确认图片" if enabled else
                               "白底图  →  九组生成  →  全部成功图片按组保存")
        hint = self._selected_options_hint()
        if hint:
            self.quote_text.set(hint + " " + self._resolution_quote(new_task=True))
        else:
            self.quote_text.set(self._resolution_quote() + (" 开启审图：不足 3 张通过图时，原组最多补生 3 轮。" if enabled else
                                                           " 未开启审图：保存全部成功图，不按合格数量补生。"))

    def _resolution_quote(self, *, new_task=False):
        """仅显示网站返回的所选尺寸报价；不可用或未读到时不猜测费用、不回退尺寸。"""
        resolution = self.resolution.get()
        prefix = "新任务 " if new_task else "均衡 "
        if resolution in self.quote_unavailable:
            return f"{prefix}{resolution}：{self.quote_unavailable[resolution]}"
        price = self.price_quotes.get(resolution)
        if price is not None:
            return f"{prefix}{resolution}：{price}积分/张，首轮45张约{price * 45}积分。"
        return f"{prefix}{resolution}：单价待网站确认，首轮45张。"

    def _selected_options_hint(self):
        """启动前说明已有任务的绑定选项，避免把默认关闭误解为改变旧批次。"""
        selected = self.product_tree.selection()
        bound = []
        for pid in selected:
            product = self.products.get(pid)
            if product is None: continue
            try:
                options = self.active_options.get(pid) or read_options(product, self.paths.tasks)
            except (OSError, ValueError, KeyError):
                return "已有任务配置待检查；上方选项只用于新任务，不会覆盖原批次。"
            if options: bound.append(options)
        if not bound: return ""
        if len(selected) != 1:
            return "已有任务保留原配置，新任务使用上方选项；继续运行不会改变已有批次的尺寸、审图开关或模型。"
        options = bound[0]
        resolution = options.get("resolution", "2K")
        if not options.get("review_enabled"):
            return f"当前商品沿用原批次：{resolution} / 关闭审图，全部成功图片按组保存；这些选项用于新任务。"
        chosen = options.get("review_profile") or options.get("profile") or {}
        model = self._model_label(chosen.get("model", DEFAULT_REVIEW_MODEL))
        locator = chosen.get("locator_effort", "xhigh" if options.get("legacy") else "medium")
        body = chosen.get("body_effort", "xhigh" if options.get("legacy") else "high")
        effort = locator if locator == body else f"定位 {locator} / 审核 {body}"
        return f"当前商品沿用原批次：{resolution} / 开启审图 / {model} / {effort}；这些选项用于新任务。"

    def _save_settings(self):
        """只保存其他长期设置；路径、模型、尺寸及审图开关仅在本次窗口有效。"""
        for key in SESSION_SETTING_KEYS: self.settings_data.pop(key, None)
        atomic_write_json(self.paths.root / "settings.json", self.settings_data)

    def _model_changed(self):
        self._update_options_text()
        self._save_settings()

    def _model_selected(self, event=None):
        """模型下拉框使用易读名称，实际调用仍绑定正式模型 ID。"""
        index = self.model_button.current()
        if index not in range(len(SUPPORTED_MODELS)): return
        self.review_model.set(SUPPORTED_MODELS[index])
        self._model_changed()

    def _resolution_changed(self):
        """变更新任务出图尺寸；已开始任务继续使用原绑定尺寸。"""
        self._update_options_text()
        self._save_settings()

    def _set_controls(self):
        """只在 Tk 主线程统一锁定操作选项；连接检查本身不阻塞单独审图。"""
        locked = self.busy or self.installing_update or self.pending_start
        state = "disabled" if locked else "normal"
        for widget in (self.path_entry, self.browse_button, self.scan_button, self.review_check,
                       self.model_button, self.resolution_button, self.settings_button, self.start_button, self.recheck_button, self.pending_button, self.regenerate_button):
            widget.configure(state=state)
        self.model_button.configure(state="disabled" if locked else "readonly")
        self.pause_button.configure(state="normal" if self.busy and not self.stop.is_set() else "disabled")

    def show_settings(self):
        """维护功能集中在单独窗口，连接/版本等信息不占主工作区域。"""
        if self.busy or self.installing_update or self.pending_start: return
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift(); return
        window = tk.Toplevel(self.root); self.settings_window = window
        window.title("搜推生图 · 设置"); window.geometry("680x270"); window.transient(self.root)
        body = ttk.Frame(window, padding=18); body.pack(fill="both", expand=True)
        ttk.Label(body, textvariable=self.account_text, wraplength=630).pack(anchor="w", pady=4)
        ttk.Label(body, textvariable=self.codex_text, wraplength=630).pack(anchor="w", pady=4)
        ttk.Label(body, text=f"程序 {APP_VERSION} · 内置规则 {POLICY_VERSION}").pack(anchor="w", pady=4)
        ttk.Label(body, textvariable=self.update_text, wraplength=630).pack(anchor="w", pady=4)
        actions = ttk.Frame(body); actions.pack(fill="x", pady=16)
        self.program_button = ttk.Button(actions, text="程序升级", command=self.upgrade_program); self.program_button.pack(side="left")
        self.access_button = ttk.Button(actions, text="只读授权", command=self.configure_updates); self.access_button.pack(side="left", padx=8)
        self.update_button = ttk.Button(actions, text="检查规则更新", command=lambda: self._background(self.check_updates)); self.update_button.pack(side="left")

    def show_pending_images(self):
        """只读列出所选商品的待确认图片及原因；不重审、不改名、不写输出文件。"""
        if self.busy or self.installing_update or self.pending_start: return
        selected = self.product_tree.selection()
        if not selected:
            messagebox.showinfo("待确认图片", "请先选择商品。", parent=self.root); return
        lines = []
        pending_count = 0
        for pid in selected:
            product = self.products.get(pid)
            if product is None: continue
            output = Path(product.source_path).parent / pid
            try:
                record = read_manifest(output, self.paths.tasks, product)
                for group in (record or {}).get("groups", []):
                    for item in group.get("saved", []):
                        if item.get("review_status") not in ("review", "error"): continue
                        pending_count += 1
                        label = "审核未完成" if item["review_status"] == "error" else "待确认"
                        lines.append(f"商品 {pid} · 组{group['row']:02} · {label}\n"
                                     f"文件：{item['filename']}\n"
                                     f"目录：{output / group['directory']}\n"
                                     f"原因：{item.get('summary') or '未记录具体原因，请检查该图片。'}")
            except (OSError, ValueError, KeyError) as error:
                lines.append(f"商品 {pid}：输出记录读取失败，未改动图片。\n{error}")
        if not lines:
            messagebox.showinfo("待确认图片", "所选商品当前没有待确认图片。", parent=self.root); return
        if self.pending_window is not None and self.pending_window.winfo_exists():
            self.pending_window.destroy()
        window = tk.Toplevel(self.root); self.pending_window = window
        window.title("搜推生图 · 待确认图片"); window.geometry("850x500"); window.minsize(600, 320); window.transient(self.root)
        body = ttk.Frame(window, padding=16); body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"所选商品共 {pending_count} 张待确认；图片仍在原目录，此处只读查看。").pack(anchor="w", pady=(0, 10))
        content = ttk.Frame(body); content.pack(fill="both", expand=True)
        self.pending_text = tk.Text(content, wrap="word", relief="flat", font=("Microsoft YaHei UI", 10), padx=10, pady=10)
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=self.pending_text.yview)
        self.pending_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y"); self.pending_text.pack(side="left", fill="both", expand=True)
        self.pending_text.insert("1.0", "\n\n".join(lines)); self.pending_text.configure(state="disabled")

    def _background(self, function):
        def worker():
            try: function()
            except Exception as error: self.events.put({"kind": "error", "message": str(error)})
        threading.Thread(target=worker, daemon=True).start()

    def browse(self):
        if self.busy: return
        chosen = filedialog.askdirectory(title="选择只放商品白底原图的文件夹", initialdir=self.input_path.get() or str(Path.home() / "Desktop"))
        if chosen: self.input_path.set(chosen); self.scan()

    def scan(self):
        """只读取所选目录的原图和已有保存清单，更新商品列表，不上传或生成。"""
        if self.busy: return
        try:
            batch = scan_inputs(self.input_path.get())
            if not batch.ready:
                raise ValueError("\n".join(f"{i.file_name}：{i.reason}" for i in batch.issues[:10]))
            self.products = {p.product_id: p for p in batch.products}
            self.product_tree.delete(*self.product_tree.get_children())
            for product in batch.products:
                count = self._count_details(product.product_id)
                self.product_tree.insert("", "end", iid=product.product_id,
                                         values=(product.product_id, f"{product.width}×{product.height}",
                                                 *(sum(x[key] for x in count.values()) for key in ("saved", "pass", "review"))))
            self.product_tree.selection_set(self.product_tree.get_children())
            self.status_text.set(f"读取到 {len(batch.products)} 个商品，已选择全部")
            self._save_settings()
        except Exception as error: messagebox.showerror("目录需要检查", str(error), parent=self.root)

    def _count_details(self, product_id):
        """读取内部账本区分已保存、通过和待确认，未审图片绝不显示成合格。"""
        source = self.products.get(product_id)
        if not source: return {}
        try:
            record = read_manifest(Path(source.source_path).parent / product_id, self.paths.tasks, source)
            result = output_counts(record)
            self.output_errors.pop(product_id, None)
            return result
        except (OSError, ValueError, KeyError) as error:
            self.output_errors[product_id] = str(error)
            return {}

    def _saved_counts(self, product_id):
        """兼容自检入口：此名称仍返回通过数，展示总保存数使用完整统计。"""
        return {row: value["pass"] for row, value in self._count_details(product_id).items()}

    def _refresh_product_count(self, product_id):
        count = self._count_details(product_id)
        source = self.products[product_id]
        self.product_tree.item(product_id, values=(product_id, f"{source.width}×{source.height}",
                               *(sum(x[key] for x in count.values()) for key in ("saved", "pass", "review"))))

    def _pending_rows(self, product_id):
        """只读辨认旧规则或尚未完成复核的组；这些旧数量不能显示为已达标。"""
        source = self.products.get(product_id)
        if not source: return set()
        try:
            record = read_manifest(Path(source.source_path).parent / product_id, self.paths.tasks, source)
            if record is None: return set()
            if record["source_sha256"] != source.sha256: return set(range(1, 10))
            expected=policy_id(display_bundle(self.paths.root,source))
            rows = {g["row"] for g in record["groups"] if any(x.get("review_status") in ("review", "error") or
                    x.get("review_status", "pass") == "pass" and x.get("policy_version") != expected for x in g["saved"])}
            return rows | {x["row"] for x in record.get("withdrawn", []) if x.get("review_status") == "pending"}
        except FileNotFoundError: return set()
        except (OSError, ValueError, KeyError): return set(range(1, 10))

    def show_selected_product(self, event=None):
        selected = self.product_tree.selection()
        self._update_options_text()
        if not selected: return
        self.current_product = selected[0]
        self._refresh_groups()

    def _refresh_groups(self):
        pid = self.current_product
        if not pid: return
        names = {}
        plan = None
        path = self.paths.tasks / f"product_{pid}_references.json"
        try:
            plan = load_plan(path)
            names = {a.row: a.group_name for a in plan.assignments}
        except (OSError, ValueError, KeyError): pass
        details = self._count_details(pid)
        pending = self._pending_rows(pid)
        try:
            options = self.active_options.get(pid) or read_options(self.products[pid], self.paths.tasks)
        except (OSError, ValueError, KeyError): options = None
        audit_enabled = options.get("review_enabled", True) if options else self.review_enabled.get()
        for row in range(1, 10):
            item = dict(self.group_states.get((pid, row), {}))
            count = details.get(row, {"saved": 0, "pass": 0, "review": 0, "unreviewed": 0})
            terminal_generation = False
            if not item:
                jobs = sorted((self.paths.tasks / pid / f"group_{row:02}").glob("round_*.json"))
                if jobs:
                    try:
                        last = json.loads(jobs[-1].read_text(encoding="utf-8"))
                        terminal_generation = (last["state"] in ("awaiting_audit", "generation_failed") and
                                               len(last.get("results", [])) == 5 and
                                               all(x.get("status") in ("done", "error") for x in last["results"]))
                        item = {"round": last["round_index"], "state": {
                            "prepared": "待提交", "generating": "生成中", "awaiting_audit": "待审核" if audit_enabled else "待保存",
                            "generation_failed": "本轮生成失败", "submission_unknown": "需核对提交"
                        }.get(last["state"], "待继续")}
                    except (OSError, ValueError, KeyError): pass
            active = self.busy and (pid, row) in self.group_states
            if pid in self.output_errors: item["state"] = "输出记录待检查"
            elif not active and row in pending: item["state"] = "有待确认图片"
            elif not active and not audit_enabled:
                if item.get("generation_finished"): pass
                elif terminal_generation: item["state"] = f"生成结束 · 已存{count['saved']}"
                elif count["unreviewed"]: item["state"] = "已保存部分 · 未审"
            elif not active and count["pass"] >= 3: item["state"] = "已达标"
            elif not active and plan and pid in self.products:
                try:
                    with use_rules(display_bundle(self.paths.root,self.products[pid])):
                        decision = group_decision(self.paths.tasks, self.products[pid], plan.assignments[row - 1])
                    if decision.action == "paused": item["state"] = "补生上限，待处理"
                    elif decision.action == "generate" and list((self.paths.tasks / pid / f"group_{row:02}").glob("round_*.json")):
                        item["state"] = "待补生"
                except (OSError, ValueError, KeyError): item["state"] = "记录待检查"
            self.group_tree.item(str(row), values=(f"{row:02}", names.get(row, "待读取"),
                                                  item.get("state", "尚未开始"), count["saved"], count["pass"],
                                                  count["review"], item.get("round", 0)))

    def check_environment(self):
        """后台核对本机 Codex 登录和网站会话，通过事件更新状态；不提交生成。"""
        try:
            cli = find_codex()
            result = subprocess.run([str(cli), "login", "status"], text=True, encoding="utf-8", errors="replace",
                                    capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ready = result.returncode == 0
            message = "Codex 已安装并登录" if ready else "请先在 Codex 中登录自己的账号"
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            ready, message = False, "未发现可用 Codex；请先安装并登录自己的账号"
        self.events.put({"kind": "codex", "ready": ready, "message": message})
        if not self._check_site_session():
            self.events.put({"kind": "connection_needed", "message": ""})

    def _check_site_session(self):
        """后台只读验证会话，不操作浏览器；价格查询失败不推翻已验证的登录。"""
        try:
            client = MusefitReadClient(self.paths.session)
            data = client.get_json("/api/web/favorites", {"mediaType": "image"})
            user = client.get_json("/api/auth/me").get("user", {})
            self.events.put({"kind": "connected", "message": f"网站已连接 · {user.get('username', '')} · {len(data.get('folders', []))} 个参考组"})
        except Exception:
            self.events.put({"kind": "disconnected", "message": "网站会话暂不可用；请在浏览器保持生图网站登录"})
            return False
        try:
            tiers = WebTransport(self.paths.session).request_json("GET", "/api/web/image-tiers")
            prices, unavailable = {}, {}
            for resolution in ("2K", "4K"):
                try: prices[resolution] = quote_balanced(tiers, resolution)
                except ValueError as error: unavailable[resolution] = str(error)
            self.events.put({"kind": "quote", "prices": prices, "unavailable": unavailable})
        except Exception: pass
        return True

    def _poll_connection(self):
        """低频只读会话检查，不周期性激活浏览器，不影响正在处理的任务。"""
        if self.closed: return
        if not self.busy and not self.connection_in_progress:
            self._background(self._check_site_session)
        self.root.after(60000, self._poll_connection)

    def connect(self):
        """会话失效时自动连接一次已有浏览器；失败仅写进度，开始任务时可再试。"""
        if self.busy or self.connection_in_progress or self.closed: return
        self.connection_in_progress = True
        self.automatic_connection_attempted = True
        self.events.put({"kind": "connection_progress", "message": "正在自动连接已有浏览器…"})
        def worker():
            try:
                connect_existing_session(self.paths.session)
                self._check_site_session()
            except Exception as error:
                self.events.put({"kind": "disconnected", "message": "自动连接暂未完成：" + str(error) +
                                 "。正常登录后开始任务会自动重试；单独审图仍可使用。"})
            finally: self.events.put({"kind": "connection_finished", "message": ""})
        self._background(worker)

    def check_updates(self):
        """后台只检查稳定发布，不在此升级正在执行的商品任务。"""
        result=self.update_service.check()
        self.events.put({"kind":"update_status","message":"",**result})

    def configure_updates(self):
        if self.busy or self.installing_update or self.pending_start: return
        show_access_dialog(self.root,self.update_service,lambda result:self.events.put({**result,"kind":"update_status"}))

    def upgrade_program(self):
        """明确点击后在空闲状态准备签名程序包，再由独立副本替换并重启。"""
        if self.busy or self.installing_update or self.pending_start:
            messagebox.showinfo("等待任务结束","请先暂停并等待当前任务结束，再升级程序。",parent=self.root); return
        if not getattr(__import__('sys'),'frozen',False):
            messagebox.showinfo("源码运行","当前运行源码；程序升级在Windows exe中使用。",parent=self.root); return
        try: offer=self.update_service.program_offer()
        except (ValueError,OSError) as error:
            messagebox.showerror("更新清单需要检查",str(error),parent=self.root); return
        if offer is None:
            self.update_text.set("程序已是当前已获取清单中的最新版本；可先检查规则更新。"); return
        self.installing_update=True; self._set_controls()
        self.update_text.set("正在下载并校验新程序，完成后重启…")
        def worker():
            try:
                from .program_update import stage_program
                staged=stage_program(self.update_service,descriptor=offer)
                self.events.put({"kind":"program_staged","staged":staged})
            except Exception as error: self.events.put({"kind":"program_failed","message":str(error)})
        self._background(worker)

    def start(self):
        """读取已选择商品并获取运行锁；后台执行统一流程，将实际结果持久保存。"""
        self._start_task(recheck_only=False)

    def recheck(self):
        """审核选中商品已保存图片；开关关闭也能使用，不要求网站连接或新增生图。"""
        self._start_task(recheck_only=True)

    def regenerate(self):
        """按已保存目录不足三张的组重新生图，始终不审图。"""
        self._start_task(recheck_only=False, regenerate_only=True)

    def _start_task(self, *, recheck_only, regenerate_only=False):
        """统一管理生成、独立审图和手动补图；三个入口共享运行锁及暂停。"""
        if self.busy or self.installing_update or self.pending_start: return
        if not self.codex_ready and not regenerate_only:
            messagebox.showinfo("先登录 Codex", "请先登录 Codex；审图及新商品服装类型识别会使用模型。", parent=self.root); return
        selected = tuple(self.product_tree.selection())
        if not selected:
            messagebox.showinfo("选择商品", "请先扫描目录并选择要处理的商品。", parent=self.root); return
        if not recheck_only and not self.connected:
            self.pending_start = True
            self.pending_regeneration = regenerate_only
            self.status_text.set("正在自动连接浏览器，连接成功后开始；请保持生图网站已登录")
            self._set_controls()
            self.connect()
            return
        directory, resolution = self.input_path.get(), self.resolution.get()
        chosen_profile = profile(self.review_model.get())
        enabled = self.review_enabled.get()
        self._save_settings()
        self.busy = True; self.stop.clear()
        self._set_controls()
        def worker():
            try:
                def notify(event):
                    self.events.put(event)
                    with (self.paths.root / "last_run.events.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                with pipeline_lock(self.paths.root):
                    runner = Pipeline(self.paths, on_event=notify, stop_event=self.stop, review_workers=4,
                                      review_enabled=enabled, review_profile=chosen_profile, resolution=resolution)
                    result = (runner.regenerate(directory, product_ids=selected) if regenerate_only else
                              runner.recheck_saved(directory, product_ids=selected) if recheck_only else
                              runner.run(directory, product_ids=selected))
                atomic_write_json(self.paths.root / "last_run_result.json", result)
                self.events.put({"kind": "task_result", "message": "已暂停，可点击原操作继续" if result["status"] == "paused" else
                                 "有组暂时不能重新生图，请查看进度区提示" if regenerate_only and result["status"] == "needs_attention" else
                                 "重新生图本轮结束；新图全部保存，未审图；仍不足3张可再次点击" if regenerate_only else
                                 "单独审图结束；已删除明确不合格图，疑难图保留待确认" if recheck_only and result["status"] == "complete" else
                                 "单独审图尚有未完成项目，请查看进度记录" if recheck_only else "本次处理结束"})
            except Exception as error:
                result = {"status": "needs_attention", "error": str(error), "products": []}
                atomic_write_json(self.paths.root / "last_run_result.json", result)
                raise
            finally: self.events.put({"kind": "worker_finished", "message": ""})
        self._background(worker)

    def pause(self):
        """发出停止新动作的信号；当前模型检查结束后保留任务和合格图以便继续。"""
        self.stop.set(); self.status_text.set("暂停请求已收到，当前检查结束后停止；已保存图片保留")
        self.pause_button.configure(state="disabled")

    def open_output(self):
        value = self.input_path.get()
        if not value: return
        directory = Path(value)
        if self.current_product and (directory / self.current_product).is_dir(): directory /= self.current_product
        if directory.is_dir(): os.startfile(str(directory))

    def _drain(self):
        if self.closed: return
        try:
            while True:
                event = self.events.get_nowait()
                kind, message = event.get("kind"), event.get("message", "")
                if kind == "codex":
                    self.codex_ready = event["ready"]; self.codex_text.set(message)
                elif kind == "connected":
                    repeated = self.connected and self.account_text.get() == message
                    self.connected = True; self.account_text.set(message)
                    if repeated: message = ""
                elif kind == "disconnected":
                    repeated = not self.connected and self.account_text.get() == message
                    self.connected = False; self.account_text.set(message)
                    if repeated: message = ""
                elif kind == "connection_progress": self.account_text.set(message)
                elif kind == "connection_needed":
                    if not self.automatic_connection_attempted: self.connect()
                elif kind == "quote":
                    prices, unavailable = event.get("prices", {}), event.get("unavailable", {})
                    repeated = self.price_quotes == prices and self.quote_unavailable == unavailable
                    self.price_quotes, self.quote_unavailable = prices, unavailable
                    self._update_options_text()
                    message = "" if repeated else self._resolution_quote()
                elif kind == "update_status":
                    self.update_text.set(event["message"])
                elif kind == "program_failed":
                    self.installing_update=False; self._set_controls()
                    self.update_text.set(message)
                elif kind == "program_staged":
                    from .program_update import launch_installer
                    import sys
                    launch_installer(event["staged"],Path(sys.executable).parent,parent_pid=os.getpid(),data_directory=self.paths.root)
                    self.closed = True; self.root.destroy(); return
                elif kind == "connection_finished":
                    self.connection_in_progress = False
                    if self.pending_start:
                        self.pending_start = False; self._set_controls()
                        if self.connected: self._start_task(recheck_only=False, regenerate_only=self.pending_regeneration)
                        else: self.status_text.set("尚未连接网站；登录后点击开始会自动重试，单独审图不受影响")
                elif kind == "worker_finished":
                    self.busy = False; self._set_controls()
                    for state in self.group_states.values():
                        if state.get("state") == "单独审图处理中":
                            state["state"] = "已审核"
                    for pid in self.products:
                        self._refresh_product_count(pid)
                    self._refresh_groups()
                elif kind == "error":
                    self.status_text.set("处理已停止，任务记录与已保存图片已保留")
                    messagebox.showerror("需要处理", message, parent=self.root)
                if event.get("product_id") in self.products:
                    pid = event["product_id"]
                    if kind == "run_options":
                        if event.get("mode") not in ("saved_review", "regenerate"): self.active_options[pid] = dict(event)
                        self._update_options_text()
                        chosen = event.get("review_profile") or event.get("profile") or {}
                        actual_model = self._model_label(chosen.get("model", self.review_model.get()))
                        mode = "重新生图（不审图）" if event.get("mode") == "regenerate" else "单独审图" if event.get("mode") == "saved_review" else "开启审图" if event.get("review_enabled") else "关闭审图"
                        message += f" · {mode}" + (f" · {actual_model}" if event.get("mode") != "regenerate" else "") + (" · 沿用原批次配置" if event.get("legacy") else "")
                        if event.get("mode") != "saved_review": message += " · " + event.get("resolution", "2K")
                    row = event.get("row")
                    if row:
                        state = self.group_states.setdefault((pid, row), {})
                        state["round"] = event.get("round_index", state.get("round", 0))
                        labels = {"submitting": "提交中", "generation_progress": "生成中",
                                  "reviewing": "逐张审核中", "image_reviewed": f"已审核 {event.get('completed', 0)}/{event.get('total', 5)}",
                                  "quality_image_reviewed": "旧图逐张复核中",
                                  "group_complete": "已达标", "group_paused": "补生上限，待处理",
                                  "generated_saved": "保存中 · 未审核", "generation_saved_complete": "保存完成 · 未审核",
                                  "saved_review_image": "单独审图处理中"}
                        if kind in labels: state["state"] = labels[kind]
                        if kind == "generation_saved_complete":
                            state["generation_finished"] = True
                            if event.get("failed"):
                                state["state"] = f"已存{event.get('successful', 0)} · 失败{event['failed']}"
                        if kind == "stream_progress":
                            prefix = "生成中 · " if event["generating"] else ""
                            state["state"] = f"{prefix}审核{event['active_reviews']} · 待审{event['queued_reviews']}"
                    if kind in ("saved", "generated_saved", "saved_review_image", "saved_review_progress"):
                        self._refresh_product_count(pid)
                    if kind in ("quality_withdrawn", "quality_rechecked"):
                        if kind == "quality_rechecked":
                            for row_number in range(1, 10): self.group_states.pop((pid, row_number), None)
                        self._refresh_product_count(pid)
                    if self.current_product == pid: self._refresh_groups()
                if message:
                    time_text = datetime.fromtimestamp(event.get("time", datetime.now().timestamp()), CN_TZ).strftime("%H:%M:%S")
                    detail = event.get("summary", "")
                    prefix = f"{event.get('product_id', '')} " + (f"组{event['row']:02} " if event.get("row") else "")
                    self.log.configure(state="normal")
                    self.log.insert("end", f"{time_text}  {prefix}{message}{'：' + detail if detail else ''}\n")
                    self.log.see("end"); self.log.configure(state="disabled")
                    if kind not in ("codex", "connected", "disconnected", "quote", "update_status", "connection_progress"): self.status_text.set(message)
        except queue.Empty: pass
        self.root.after(100, self._drain)

    def close(self):
        if self.busy:
            self.pause()
            messagebox.showinfo("正在暂停", "请等待当前检查结束后再关闭窗口，避免中断图片记录。", parent=self.root)
            return
        self.closed = True
        self.root.destroy()


def launch(data_directory, initial_directory=None, *, smoke_output=None):
    """启动工作台；交付自检可截图并自动关闭，绝不触发生成。"""
    root = tk.Tk()
    app = Workbench(root, data_directory, initial_directory)
    if smoke_output:
        import time
        capture_started=time.monotonic()
        def capture():
            if app.update_text.get().startswith("正在检查") and time.monotonic()-capture_started<90:
                root.after(1000,capture); return
            from .window_capture import capture_own_window
            target = Path(smoke_output); target.mkdir(parents=True, exist_ok=True)
            root.update_idletasks()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            width, height = root.winfo_width(), root.winfo_height()
            capture_own_window(root, target / "preview.png")
            result = {"packaged": bool(getattr(__import__('sys'), 'frozen', False)),
                      "window_created": True, "native_window_capture": True, "size": [width, height], "products": len(app.products),
                      "group_rows": len(app.group_tree.get_children()), "connected": app.connected,
                      "visible_group_rows": [i for i in range(1, 10) if app.group_tree.bbox(str(i)) and
                                             sum(app.group_tree.bbox(str(i))[1::2]) <= app.group_tree.winfo_height()],
                      "group_scrollbar_present": app.group_scrollbar.winfo_exists() == 1,
                      "review_model": app.review_model.get(), "review_enabled": app.review_enabled.get(), "policy_version": POLICY_VERSION,
                      "model_choices": list(SUPPORTED_MODELS), "settings_button_present": app.settings_button.winfo_exists() == 1,
                      "resolution": app.resolution.get(), "resolution_choices": ["2K", "4K"],
                      "concurrency_selector_present": hasattr(app, "workers_combo"),
                      "options_row_white_and_borderless": all(str(w.cget("style")).startswith("Options.") for w in
                                                               (app.review_check, app.model_button, app.resolution_button)),
                      "log_size": [app.log.winfo_width(), app.log.winfo_height()],
                      "log_scrollbar_present": app.log_scrollbar.winfo_exists() == 1,
                      "recheck_button_present": app.recheck_button.winfo_exists() == 1,
                      "pending_button_present": app.pending_button.winfo_exists() == 1,
                      "group_values": [list(app.group_tree.item(str(i), "values")) for i in range(1, 10)],
                      "current_counts": {pid: sum(app._saved_counts(pid).values()) for pid in app.products},
                      "output_counts": {pid: app._count_details(pid) for pid in app.products},
                      "app_version":APP_VERSION,"private_update_controls":"settings_window","update_status":app.update_text.get(),
                      "codex_ready": app.codex_ready, "generation_started": app.busy}
            (target / "smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            root.after(500, root.destroy)
        root.after(5000, capture)
    root.mainloop()
