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
from .stage_d_web_generation import group_decision
from .web_session import MusefitReadClient
from .web_transport import WebTransport
from .update_service import UpdateService
from .update_dialog import show_access_dialog
from .update_config import APP_VERSION,display_bundle
from .rule_runtime import use_rules
from .rule_store import policy_id


CN_TZ = timezone(timedelta(hours=8))
COLORS = {"background": "#f3f5f9", "panel": "#ffffff", "ink": "#18253c", "muted": "#66758b",
          "blue": "#2463eb", "green": "#16866d", "orange": "#b56b16", "red": "#bc3b45"}


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
        self.update_service = UpdateService(self.paths.root)
        self.update_text = tk.StringVar(value="正在检查私有规则更新…")
        self.current_product = None
        self.products = {}
        self.group_states = {}
        self.input_path = tk.StringVar()
        self.workers = tk.StringVar(value="2")
        self.account_text = tk.StringVar(value="正在检查本机连接…")
        self.codex_text = tk.StringVar(value="正在检查 Codex…")
        self.status_text = tk.StringVar(value="选择白底图文件夹，检查完成后开始")
        self.quote_text = tk.StringVar(value="每商品首轮 45 张；每组不足 3 张时，原组最多补生 3 轮。")
        settings = self.paths.root / "settings.json"
        if settings.exists():
            try: self.input_path.set(json.loads(settings.read_text(encoding="utf-8")).get("input_directory", ""))
            except (OSError, ValueError): pass
        if initial_directory: self.input_path.set(str(initial_directory))
        self._build()
        if self.input_path.get(): self.scan()
        self.root.after(100, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._background(self.check_environment)
        self._background(self.check_updates)

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
        style.configure("Treeview", rowheight=28, background="white", fieldbackground="white", foreground=COLORS["ink"])
        style.configure("Treeview.Heading", background="#eaf0fa", font=("Microsoft YaHei UI", 10, "bold"))
        main = ttk.Frame(self.root, padding=24); main.pack(fill="both", expand=True)
        ttk.Label(main, text="搜推生图", font=("Microsoft YaHei UI", 23, "bold")).pack(anchor="w")
        ttk.Label(main, text="白底图  →  九组生成  →  逐张审核  →  合格图归档", style="Muted.TLabel").pack(anchor="w", pady=(4, 17))
        card = ttk.Frame(main, style="Card.TFrame", padding=16); card.pack(fill="x")
        ttk.Label(card, text="白底图文件夹", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(card, textvariable=self.input_path); entry.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        ttk.Button(card, text="选择文件夹", command=self.browse).grid(row=1, column=1, padx=(10, 0), pady=(9, 0))
        self.scan_button = ttk.Button(card, text="扫描目录", command=self.scan); self.scan_button.grid(row=1, column=2, padx=(8, 0), pady=(9, 0))
        card.columnconfigure(0, weight=1)
        connection = ttk.Frame(main); connection.pack(fill="x", pady=(12, 5))
        self.connection_label = ttk.Label(connection, textvariable=self.account_text); self.connection_label.pack(side="left")
        self.connect_button = ttk.Button(connection, text="连接当前浏览器账号", command=self.connect); self.connect_button.pack(side="right")
        ttk.Label(main, textvariable=self.codex_text, style="Muted.TLabel").pack(anchor="w", pady=(2, 2))
        ttk.Label(main, text=f"程序 {APP_VERSION} · 审核模型：{DEFAULT_REVIEW_MODEL} · 初始规则 {POLICY_VERSION}", style="Muted.TLabel").pack(anchor="w", pady=(0, 5))
        updates=ttk.Frame(main); updates.pack(fill="x",pady=(0,10))
        ttk.Label(updates,textvariable=self.update_text,wraplength=650,style="Muted.TLabel").pack(side="left")
        self.update_button=ttk.Button(updates,text="检查规则更新",command=lambda:self._background(self.check_updates)); self.update_button.pack(side="right")
        self.access_button=ttk.Button(updates,text="只读授权",command=self.configure_updates); self.access_button.pack(side="right",padx=6)
        self.program_button=ttk.Button(updates,text="程序升级",command=self.upgrade_program); self.program_button.pack(side="right")
        panes = ttk.Panedwindow(main, orient="horizontal"); panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, style="Card.TFrame", padding=12)
        right = ttk.Frame(panes, style="Card.TFrame", padding=12)
        panes.add(left, weight=2); panes.add(right, weight=3)
        ttk.Label(left, text="商品 · 可多选", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        self.product_tree = ttk.Treeview(left, columns=("id", "size", "saved"), show="headings", selectmode="extended", height=9)
        for col, title, width in (("id", "商品 ID", 155), ("size", "原图尺寸", 105), ("saved", "已合格", 65)):
            self.product_tree.heading(col, text=title); self.product_tree.column(col, width=width, anchor="center")
        self.product_tree.pack(fill="both", expand=True)
        self.product_tree.bind("<<TreeviewSelect>>", self.show_selected_product)
        ttk.Label(right, text="当前商品 · 九个参考组", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        self.group_tree = ttk.Treeview(right, columns=("group", "name", "state", "saved", "round"), show="headings", height=9)
        for col, title, width in (("group", "组号", 48), ("name", "参考分组", 115), ("state", "当前进度", 155), ("saved", "合格", 48), ("round", "补生", 48)):
            self.group_tree.heading(col, text=title); self.group_tree.column(col, width=width, anchor="center")
        self.group_tree.pack(fill="both", expand=True)
        for i in range(1, 10): self.group_tree.insert("", "end", iid=str(i), values=(f"{i:02}", "待读取", "尚未开始", 0, 0))
        controls = ttk.Frame(main); controls.pack(fill="x", pady=(14, 6))
        self.start_button = ttk.Button(controls, text="开始 / 继续", style="Primary.TButton", command=self.start); self.start_button.pack(side="left")
        self.recheck_button = ttk.Button(controls, text="复核旧图片", command=self.recheck); self.recheck_button.pack(side="left", padx=(9, 0))
        self.pause_button = ttk.Button(controls, text="暂停新任务", command=self.pause, state="disabled"); self.pause_button.pack(side="left", padx=9)
        ttk.Button(controls, text="查看图片目录", command=self.open_output).pack(side="left")
        ttk.Label(controls, text="同时审核").pack(side="right", padx=(10, 6))
        ttk.Combobox(controls, textvariable=self.workers, values=("1", "2", "4"), state="readonly", width=4).pack(side="right")
        ttk.Label(main, textvariable=self.quote_text, style="Muted.TLabel").pack(anchor="w", pady=(0, 5))
        ttk.Label(main, textvariable=self.status_text, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(2, 5))
        self.log = tk.Text(main, height=6, wrap="word", relief="flat", bg="white", fg=COLORS["muted"],
                           font=("Microsoft YaHei UI", 9), padx=10, pady=8, state="disabled")
        self.log.pack(fill="x")

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
                count = self._saved_counts(product.product_id)
                self.product_tree.insert("", "end", iid=product.product_id,
                                         values=(product.product_id, f"{product.width}×{product.height}", sum(count.values())))
            self.product_tree.selection_set(self.product_tree.get_children())
            self.status_text.set(f"读取到 {len(batch.products)} 个商品，已选择全部")
            (self.paths.root / "settings.json").write_text(json.dumps({"input_directory": batch.input_directory}, ensure_ascii=False), encoding="utf-8")
        except Exception as error: messagebox.showerror("目录需要检查", str(error), parent=self.root)

    def _saved_counts(self, product_id):
        source = self.products.get(product_id)
        if not source: return {}
        path = Path(source.source_path).parent / product_id / ".搜推生图.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["source_sha256"] != source.sha256: return {}
            expected=policy_id(display_bundle(self.paths.root,source))
            return {g["row"]: sum(x.get("policy_version") == expected for x in g["saved"]) for g in record["groups"]}
        except (OSError, ValueError, KeyError): return {}

    def _pending_rows(self, product_id):
        """只读辨认旧规则或尚未完成复核的组；这些旧数量不能显示为已达标。"""
        source = self.products.get(product_id)
        if not source: return set()
        try:
            record = json.loads((Path(source.source_path).parent / product_id / ".搜推生图.json").read_text(encoding="utf-8"))
            if record["source_sha256"] != source.sha256: return set(range(1, 10))
            expected=policy_id(display_bundle(self.paths.root,source))
            rows = {g["row"] for g in record["groups"] if any(x.get("policy_version") != expected for x in g["saved"])}
            return rows | {x["row"] for x in record.get("withdrawn", []) if x.get("review_status") == "pending"}
        except FileNotFoundError: return set()
        except (OSError, ValueError, KeyError): return set(range(1, 10))

    def show_selected_product(self, event=None):
        selected = self.product_tree.selection()
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
        saved = self._saved_counts(pid)
        pending = self._pending_rows(pid)
        for row in range(1, 10):
            item = dict(self.group_states.get((pid, row), {}))
            if not item:
                jobs = sorted((self.paths.tasks / pid / f"group_{row:02}").glob("round_*.json"))
                if jobs:
                    try:
                        last = json.loads(jobs[-1].read_text(encoding="utf-8"))
                        item = {"round": last["round_index"], "state": {
                            "prepared": "待提交", "generating": "生成中", "awaiting_audit": "待逐图审核",
                            "generation_failed": "本轮生成失败", "submission_unknown": "需核对提交"
                        }.get(last["state"], "待继续")}
                    except (OSError, ValueError, KeyError): pass
            if row in pending: item["state"] = "旧图待复核"
            elif saved.get(row, 0) >= 3: item["state"] = "已达标"
            elif plan and pid in self.products:
                try:
                    with use_rules(display_bundle(self.paths.root,self.products[pid])):
                        decision = group_decision(self.paths.tasks, self.products[pid], plan.assignments[row - 1])
                    if decision.action == "paused": item["state"] = "补生上限，待处理"
                    elif decision.action == "generate" and list((self.paths.tasks / pid / f"group_{row:02}").glob("round_*.json")):
                        item["state"] = "待补生"
                except (OSError, ValueError, KeyError): item["state"] = "记录待检查"
            self.group_tree.item(str(row), values=(f"{row:02}", names.get(row, "待读取"),
                                                  item.get("state", "已达标" if saved.get(row, 0) >= 3 else "待继续" if saved.get(row, 0) else "尚未开始"),
                                                  saved.get(row, 0), item.get("round", 0)))

    def check_environment(self):
        """后台核对本机 Codex 登录和网站会话，通过事件更新状态；不提交生成。"""
        try:
            cli = find_codex()
            result = subprocess.run([str(cli), "login", "status"], text=True, encoding="utf-8", errors="replace",
                                    capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ready = result.returncode == 0
            message = "Codex 已安装并登录" if ready else "请先在 Codex 中登录自己的账号"
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            ready, message = False, "未发现可用 Codex；请先安装并登录自己的账号，再连接"
        self.events.put({"kind": "codex", "ready": ready, "message": message})
        try:
            client = MusefitReadClient(self.paths.session)
            data = client.get_json("/api/web/favorites", {"mediaType": "image"})
            user = client.get_json("/api/auth/me").get("user", {})
            self.events.put({"kind": "connected", "message": f"网站已连接 · {user.get('username', '')} · {len(data.get('folders', []))} 个参考组"})
            tiers = WebTransport(self.paths.session).request_json("GET", "/api/web/image-tiers")
            prices = [t.get("prices", {}).get("2K") for t in tiers.get("tiers", []) if t.get("id") == "balanced"]
            if prices and isinstance(prices[0], int):
                self.events.put({"kind": "quote", "message": f"网站当前均衡 2K 为 {prices[0]} 积分/张；首轮 45 张约 {prices[0] * 45} 积分/商品，补生另计。"})
        except Exception:
            self.events.put({"kind": "disconnected", "message": "尚未连接网站，请先在现有浏览器正常登录后连接"})

    def connect(self):
        """明确点击后连接已有浏览器，把结果回传界面；会话只加密保存在本机。"""
        if self.busy: return
        self.connect_button.configure(state="disabled")
        self.account_text.set("正在连接已有浏览器…")
        def worker():
            try:
                connect_existing_session(self.paths.session)
                self.check_environment()
            finally: self.events.put({"kind": "connection_finished", "message": ""})
        self._background(worker)

    def check_updates(self):
        """后台只检查稳定发布，不在此升级正在执行的商品任务。"""
        result=self.update_service.check()
        self.events.put({"kind":"update_status","message":"",**result})

    def configure_updates(self):
        if self.busy or self.installing_update: return
        show_access_dialog(self.root,self.update_service,lambda result:self.events.put({**result,"kind":"update_status"}))

    def upgrade_program(self):
        """明确点击后在空闲状态准备签名程序包，再由独立副本替换并重启。"""
        if self.busy or self.installing_update:
            messagebox.showinfo("等待任务结束","请先暂停并等待当前任务结束，再升级程序。",parent=self.root); return
        if not getattr(__import__('sys'),'frozen',False):
            messagebox.showinfo("源码运行","当前运行源码；程序升级在Windows exe中使用。",parent=self.root); return
        try: offer=self.update_service.program_offer()
        except (ValueError,OSError) as error:
            messagebox.showerror("更新清单需要检查",str(error),parent=self.root); return
        if offer is None:
            self.update_text.set("程序已是当前已获取清单中的最新版本；可先检查规则更新。"); return
        self.installing_update=True; self.start_button.configure(state="disabled"); self.recheck_button.configure(state="disabled")
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
        """只复核选中商品的旧图片；只需 Codex，不要求连接网站，不提交生成。"""
        self._start_task(recheck_only=True)

    def _start_task(self, *, recheck_only):
        """统一管理两类明确动作的选择、运行锁和暂停；复核分支不进入网站流程。"""
        if self.busy or self.installing_update: return
        if not recheck_only and not self.connected:
            messagebox.showinfo("先连接网站", "请保持生图网站已登录，再点“连接当前浏览器账号”。", parent=self.root); return
        if not self.codex_ready:
            messagebox.showinfo("先登录 Codex", "图片审核需要本机安装并登录 Codex。", parent=self.root); return
        selected = tuple(self.product_tree.selection())
        if not selected:
            messagebox.showinfo("选择商品", "请先扫描目录并选择要处理的商品。", parent=self.root); return
        directory, workers = self.input_path.get(), int(self.workers.get())
        self.busy = True; self.stop.clear()
        self.start_button.configure(state="disabled"); self.scan_button.configure(state="disabled")
        self.recheck_button.configure(state="disabled")
        self.connect_button.configure(state="disabled"); self.pause_button.configure(state="normal")
        def worker():
            try:
                def notify(event):
                    self.events.put(event)
                    with (self.paths.root / "last_run.events.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                with pipeline_lock(self.paths.root):
                    runner = Pipeline(self.paths, on_event=notify, stop_event=self.stop, review_workers=workers, model=DEFAULT_REVIEW_MODEL)
                    result = (runner.recheck_saved(directory, product_ids=selected) if recheck_only else
                              runner.run(directory, product_ids=selected))
                (self.paths.root / "last_run_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                self.events.put({"kind": "task_result", "message": "已暂停，可点击原操作继续" if result["status"] == "paused" else
                                 "旧图复核结束；各组当前合格数量已更新" if recheck_only else "本次处理结束"})
            except Exception as error:
                result = {"status": "needs_attention", "error": str(error), "products": []}
                (self.paths.root / "last_run_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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
        try:
            while True:
                event = self.events.get_nowait()
                kind, message = event.get("kind"), event.get("message", "")
                if kind == "codex":
                    self.codex_ready = event["ready"]; self.codex_text.set(message)
                elif kind == "connected":
                    self.connected = True; self.account_text.set(message)
                elif kind == "disconnected":
                    self.connected = False; self.account_text.set(message)
                elif kind == "quote": self.quote_text.set(message)
                elif kind == "update_status":
                    self.update_text.set(event["message"])
                elif kind == "program_failed":
                    self.installing_update=False; self.start_button.configure(state="normal"); self.recheck_button.configure(state="normal")
                    self.update_text.set(message)
                elif kind == "program_staged":
                    from .program_update import launch_installer
                    import sys
                    launch_installer(event["staged"],Path(sys.executable).parent,parent_pid=os.getpid(),data_directory=self.paths.root)
                    self.root.destroy(); return
                elif kind == "connection_finished": self.connect_button.configure(state="normal")
                elif kind == "worker_finished":
                    self.busy = False; self.start_button.configure(state="normal"); self.scan_button.configure(state="normal")
                    self.recheck_button.configure(state="normal")
                    self.connect_button.configure(state="normal"); self.pause_button.configure(state="disabled")
                    for pid in self.products:
                        values = list(self.product_tree.item(pid, "values")); values[2] = sum(self._saved_counts(pid).values())
                        self.product_tree.item(pid, values=values)
                    self._refresh_groups()
                elif kind == "error":
                    self.status_text.set("处理已停止，任务记录与合格图已保留")
                    messagebox.showerror("需要处理", message, parent=self.root)
                if event.get("product_id") in self.products:
                    pid = event["product_id"]
                    row = event.get("row")
                    if row:
                        state = self.group_states.setdefault((pid, row), {})
                        state["round"] = event.get("round_index", state.get("round", 0))
                        labels = {"submitting": "提交中", "generation_progress": "生成中",
                                  "reviewing": "逐张审核中", "image_reviewed": f"已审核 {event.get('completed', 0)}/{event.get('total', 5)}",
                                  "quality_image_reviewed": "旧图逐张复核中",
                                  "group_complete": "已达标", "group_paused": "补生上限，待处理"}
                        if kind in labels: state["state"] = labels[kind]
                    if kind == "saved":
                        values = list(self.product_tree.item(pid, "values")); values[2] = event["total_saved"]
                        self.product_tree.item(pid, values=values)
                    if kind in ("quality_withdrawn", "quality_rechecked"):
                        if kind == "quality_rechecked":
                            for row_number in range(1, 10): self.group_states.pop((pid, row_number), None)
                        values = list(self.product_tree.item(pid, "values")); values[2] = sum(self._saved_counts(pid).values())
                        self.product_tree.item(pid, values=values)
                    if self.current_product == pid: self._refresh_groups()
                if message:
                    time_text = datetime.fromtimestamp(event.get("time", datetime.now().timestamp()), CN_TZ).strftime("%H:%M:%S")
                    detail = event.get("summary", "")
                    prefix = f"{event.get('product_id', '')} " + (f"组{event['row']:02} " if event.get("row") else "")
                    self.log.configure(state="normal")
                    self.log.insert("end", f"{time_text}  {prefix}{message}{'：' + detail if detail else ''}\n")
                    self.log.see("end"); self.log.configure(state="disabled")
                    if kind not in ("codex", "connected", "disconnected", "quote", "update_status"): self.status_text.set(message)
        except queue.Empty: pass
        self.root.after(100, self._drain)

    def close(self):
        if self.busy:
            self.pause()
            messagebox.showinfo("正在暂停", "请等待当前检查结束后再关闭窗口，避免中断图片记录。", parent=self.root)
            return
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
                      "review_model": DEFAULT_REVIEW_MODEL, "policy_version": POLICY_VERSION,
                      "recheck_button_present": app.recheck_button.winfo_exists() == 1,
                      "group_values": [list(app.group_tree.item(str(i), "values")) for i in range(1, 10)],
                      "current_counts": {pid: sum(app._saved_counts(pid).values()) for pid in app.products},
                      "app_version":APP_VERSION,"private_update_controls":True,"update_status":app.update_text.get(),
                      "codex_ready": app.codex_ready, "generation_started": app.busy}
            (target / "smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            root.after(500, root.destroy)
        root.after(5000, capture)
    root.mainloop()
