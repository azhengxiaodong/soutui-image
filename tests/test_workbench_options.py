"""工作台模式、离线审图及自动连接控制；全部网络/模型入口隔离，不提交生图。"""

import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from soutui_image.desktop_app import Workbench


class WorkbenchOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.inputs = self.base / "inputs"; self.inputs.mkdir()
        Image.new("RGB", (20, 20), "white").save(self.inputs / "001.png")
        self.data = self.base / "data"; self.data.mkdir()
        self.jobs = []
        self.roots = []
        self.addCleanup(self.close_roots)
        self.background = patch.object(Workbench, "_background", autospec=True,
                                       side_effect=lambda app, function: self.jobs.append(function))
        self.background.start(); self.addCleanup(self.background.stop)
        self.messages = patch("soutui_image.desktop_app.messagebox")
        self.dialogs = self.messages.start(); self.addCleanup(self.messages.stop)

    def close_roots(self):
        for root in reversed(self.roots):
            try:
                for callback in root.tk.call("after", "info"):
                    root.after_cancel(callback)
                root.destroy()
            except tk.TclError: pass

    def app(self):
        root = tk.Tk(); root.withdraw(); self.roots.append(root)
        result = Workbench(root, self.data, self.inputs)
        root.update_idletasks()
        result.product_tree.selection_set("001")
        result.current_product = "001"
        return result

    def test_review_and_model_reset_on_restart_but_not_during_current_scan(self):
        (self.data / "settings.json").write_text(json.dumps({"review_enabled": True,
            "review_model": "gpt-6.1-terra", "unrelated": "keep"}), encoding="utf-8")
        first = self.app()
        self.assertFalse(first.review_enabled.get())
        self.assertEqual(first.review_model.get(), "gpt-6.1-sol")
        first.review_model.set("gpt-6.1-terra"); first._model_changed()
        first.review_enabled.set(True)
        first.scan()
        self.assertEqual(first.review_model.get(), "gpt-6.1-terra")
        self.assertTrue(first.review_enabled.get())
        settings = json.loads((self.data / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("review_enabled", settings)
        self.assertEqual(settings["unrelated"], "keep")
        second = self.app()
        self.assertFalse(second.review_enabled.get())
        self.assertEqual(second.review_model.get(), "gpt-6.1-sol")

    def test_resolution_resets_to_2k_after_restart(self):
        first = self.app()
        self.assertEqual(first.resolution.get(), "2K")
        self.assertFalse(hasattr(first, "workers_combo"))
        first.resolution.set("4K"); first._resolution_changed()
        second = self.app()
        self.assertEqual(first.resolution.get(), "4K")
        self.assertEqual(second.resolution.get(), "2K")
        settings = json.loads((self.data / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("resolution", settings)
        self.assertNotIn("review_workers", settings)

    def test_normal_start_is_blank_and_preserves_business_and_login_files(self):
        settings = {"input_directory": str(self.inputs), "review_model": "gpt-6.1-terra",
                    "resolution": "4K", "review_enabled": True, "review_workers": 1, "unrelated": "keep"}
        (self.data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        protected = {self.data / "last_run.events.jsonl": b"previous progress\n",
                     self.data / "last_run_result.json": b'{"previous": true}',
                     self.data / "tasks" / "pending.json": b'{"round_index": 2}',
                     self.data / "browser-session.json": b'{"fixture": "login"}',
                     self.inputs / "001.png": (self.inputs / "001.png").read_bytes()}
        for path, content in protected.items():
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
        root = tk.Tk(); root.withdraw(); self.roots.append(root)
        with patch("soutui_image.desktop_app.scan_inputs") as scan:
            app = Workbench(root, self.data)
            root.update_idletasks()
            scan.assert_not_called()
        self.assertEqual(app.input_path.get(), "")
        self.assertEqual(app.product_tree.get_children(), ())
        self.assertEqual(len(app.group_tree.get_children()), 9)
        for item in app.group_tree.get_children():
            self.assertEqual(tuple(map(str, app.group_tree.item(item, "values")[1:])),
                             ("待读取", "尚未开始", "0", "0", "0", "0"))
        self.assertEqual(app.log.get("1.0", "end-1c"), "")
        self.assertEqual(app.products, {})
        self.assertEqual(app.group_states, {})
        self.assertIsNone(app.current_product)
        self.assertFalse(app.review_enabled.get())
        self.assertEqual(app.review_model.get(), "gpt-6.1-sol")
        self.assertEqual(app.resolution.get(), "2K")
        self.assertEqual(json.loads((self.data / "settings.json").read_text(encoding="utf-8")), {"unrelated": "keep"})
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        self.assertEqual({job.__name__ for job in self.jobs}, {"check_environment", "check_updates"})

    def test_size_changes_price_from_actual_tier_response_and_unavailable_never_falls_back(self):
        app = self.app()
        tiers = {"success": True, "enabled": True, "features": ["model-dressing"],
                 "tiers": [{"id": "balanced", "available": True, "availableResolutions": ["2K", "4K"],
                            "prices": {"2K": 6, "4K": 10}}]}
        def response(path, *args):
            return {"folders": []} if path == "/api/web/favorites" else {"user": {"username": "test"}}
        with patch("soutui_image.desktop_app.MusefitReadClient") as client, \
             patch("soutui_image.desktop_app.WebTransport") as transport:
            client.return_value.get_json.side_effect = response
            transport.return_value.request_json.return_value = tiers
            self.assertTrue(app._check_site_session()); app._drain()
            self.assertIn("6积分/张", app.quote_text.get())
            app.resolution.set("4K"); app._resolution_changed()
            self.assertIn("10积分/张", app.quote_text.get())
            self.assertIn("450积分", app.quote_text.get())
            tiers["tiers"][0]["availableResolutions"] = ["2K"]
            self.assertTrue(app._check_site_session()); app._drain()
        self.assertEqual(app.resolution.get(), "4K")
        self.assertIn("不能使用均衡模式 4K", app.quote_text.get())
        self.assertNotIn("6积分/张", app.quote_text.get())

    def test_generation_uses_selected_resolution_and_fixed_four_workers(self):
        (self.data / "settings.json").write_text(json.dumps({"review_workers": 1}), encoding="utf-8")
        app = self.app(); app.codex_ready = app.connected = True; self.jobs.clear()
        app.resolution.set("4K")
        runner = Mock(); runner.run.return_value = {"status": "complete", "products": []}
        with patch("soutui_image.desktop_app.Pipeline", return_value=runner) as factory:
            app.start()
            self.assertEqual(str(app.resolution_button.cget("state")), "disabled")
            self.jobs.pop()()
            self.assertEqual(factory.call_args.kwargs["resolution"], "4K")
            self.assertEqual(factory.call_args.kwargs["review_workers"], 4)
            app._drain()
        self.assertEqual(str(app.resolution_button.cget("state")), "normal")

    def test_legacy_size_hint_remains_2k_when_new_selection_is_4k(self):
        app = self.app(); app.resolution.set("4K")
        options = {"review_enabled": False, "legacy": True}
        with patch("soutui_image.desktop_app.read_options", return_value=options):
            app._update_options_text()
        self.assertIn("沿用原批次：2K", app.quote_text.get())
        self.assertIn("新任务 4K", app.quote_text.get())
        self.assertEqual(app.resolution.get(), "4K")

    def test_saved_unreviewed_and_pending_never_count_as_pass(self):
        app = self.app()
        record = {"source_sha256": app.products["001"].sha256,
                  "groups": [{"row": 1, "saved": [
                      {"review_status": "unreviewed"}, {"review_status": "pass"},
                      {"review_status": "review"}, {"review_status": "error"}]}]}
        with patch("soutui_image.desktop_app.read_manifest", return_value=record):
            app._refresh_product_count("001")
            self.assertEqual(app.product_tree.item("001", "values")[-3:], ("4", "1", "2"))
            self.assertEqual(app._saved_counts("001"), {1: 1})

    def test_standalone_review_runs_with_checkbox_off_and_browser_disconnected(self):
        app = self.app(); app.codex_ready = True; self.jobs.clear()
        app.review_model.set("gpt-6.1-terra")
        runner = Mock()
        runner.recheck_saved.return_value = {"status": "complete", "products": []}
        with patch("soutui_image.desktop_app.Pipeline", return_value=runner) as factory, \
             patch("soutui_image.desktop_app.connect_existing_session", side_effect=AssertionError("独立审图不需连接网站")):
            app.recheck()
            self.assertTrue(app.busy)
            for widget in (app.path_entry, app.browse_button, app.scan_button, app.review_check,
                           app.model_button, app.resolution_button):
                self.assertEqual(str(widget.cget("state")), "disabled")
            self.jobs.pop()()
            self.assertFalse(factory.call_args.kwargs["review_enabled"])
            self.assertEqual(factory.call_args.kwargs["review_profile"].model, "gpt-6.1-terra")
            runner.recheck_saved.assert_called_once_with(str(self.inputs), product_ids=("001",))
            runner.run.assert_not_called()
            app._drain()
        self.assertFalse(app.busy)
        self.assertEqual(str(app.model_button.cget("state")), "readonly")
        self.assertEqual(str(app.resolution_button.cget("state")), "normal")

    def test_auto_bridge_is_once_and_failure_is_logged_without_dialog(self):
        app = self.app(); self.jobs.clear()
        app.events.put({"kind": "connection_needed"})
        app.events.put({"kind": "connection_needed"})
        app._drain()
        self.assertEqual(len(self.jobs), 1)
        with patch("soutui_image.desktop_app.connect_existing_session", side_effect=RuntimeError("未登录")) as bridge:
            self.jobs.pop()()
            app._drain()
        bridge.assert_called_once()
        self.assertFalse(app.connection_in_progress)
        self.assertIn("未登录", app.log.get("1.0", "end"))
        self.dialogs.showerror.assert_not_called()
        app.events.put({"kind": "connection_needed"}); app._drain()
        self.assertFalse(self.jobs)

    def test_periodic_check_only_reads_session_without_opening_browser(self):
        app = self.app(); self.jobs.clear()
        with patch.object(app, "_check_site_session", return_value=False) as check, \
             patch("soutui_image.desktop_app.connect_existing_session", side_effect=AssertionError("轮询不应激活浏览器")):
            app._poll_connection()
            self.jobs.pop()()
        check.assert_called_once()

    def test_begin_waits_for_auto_connection_and_failure_does_not_start_pipeline(self):
        app = self.app(); app.codex_ready = True; self.jobs.clear()
        with patch("soutui_image.desktop_app.Pipeline", side_effect=AssertionError("连接失败不能生图")), \
             patch("soutui_image.desktop_app.connect_existing_session", side_effect=RuntimeError("尚未登录")):
            app.start()
            self.assertTrue(app.pending_start)
            self.assertFalse(app.busy)
            self.jobs.pop()()
            app._drain()
        self.assertFalse(app.pending_start)
        self.assertFalse(app.busy)
        self.assertEqual(str(app.start_button.cget("state")), "normal")
        self.dialogs.showerror.assert_not_called()

    def test_bound_options_are_displayed_without_overwriting_new_task_preferences(self):
        app = self.app()
        app.review_model.set("gpt-6.1-terra")
        app.events.put({"kind": "run_options", "product_id": "001", "review_enabled": True,
                        "legacy": True, "review_profile": {"model": "gpt-6.1-sol"}, "message": "续跑"})
        app._drain()
        self.assertFalse(app.review_enabled.get())
        self.assertEqual(app.review_model.get(), "gpt-6.1-terra")
        self.assertTrue(app.active_options["001"]["review_enabled"])
        self.assertIn("沿用原批次配置", app.log.get("1.0", "end"))
        self.assertIn("GPT-6.1 Sol", app.log.get("1.0", "end"))

    def test_successful_auto_connection_starts_only_the_requested_generation(self):
        app = self.app(); app.codex_ready = True; self.jobs.clear()
        runner = Mock(); runner.run.return_value = {"status": "complete", "products": []}
        def ready():
            app.events.put({"kind": "connected", "message": "测试账号已连接"})
            return True
        with patch("soutui_image.desktop_app.connect_existing_session"), \
             patch.object(app, "_check_site_session", side_effect=ready), \
             patch("soutui_image.desktop_app.Pipeline", return_value=runner) as factory:
            app.start()
            runner.run.assert_not_called()
            self.jobs.pop()()
            app._drain()
            self.assertTrue(app.busy)
            self.assertFalse(app.pending_start)
            self.jobs.pop()()
            self.assertFalse(factory.call_args.kwargs["review_enabled"])
            runner.run.assert_called_once_with(str(self.inputs), product_ids=("001",))
            runner.recheck_saved.assert_not_called()
            app._drain()

    def test_download_completion_stays_completed_after_worker_finishes(self):
        app = self.app()
        app.active_options["001"] = {"review_enabled": False}
        record = {"source_sha256": app.products["001"].sha256,
                  "groups": [{"row": 1, "saved": [{"review_status": "unreviewed"}]}]}
        with patch("soutui_image.desktop_app.read_manifest", return_value=record):
            app.events.put({"kind": "generation_saved_complete", "product_id": "001", "row": 1,
                            "successful": 1, "failed": 0})
            app.events.put({"kind": "worker_finished"})
            app._drain()
        values = app.group_tree.item("1", "values")
        self.assertEqual(values[2], "保存完成 · 未审核")
        self.assertEqual(values[3:6], ("1", "0", "0"))

    def test_large_log_and_settings_keep_maintenance_controls_off_main(self):
        app = self.app()
        self.assertFalse(hasattr(app, "connect_button"))
        self.assertFalse(hasattr(app, "program_button"))
        self.assertGreaterEqual(int(app.log.cget("height")), 8)
        self.assertTrue(app.log_scrollbar.winfo_exists())
        app.show_settings()
        self.assertTrue(app.program_button.winfo_exists())
        self.assertTrue(app.program_button.winfo_toplevel() is app.settings_window)

    def test_default_size_shows_nine_groups_and_minimum_size_can_scroll(self):
        app = self.app()
        app.root.geometry("1140x920+20000+20000"); app.root.deiconify(); app.root.update(); app.root.update_idletasks()
        last = app.group_tree.bbox("9")
        self.assertTrue(last)
        self.assertLessEqual(last[1] + last[3], app.group_tree.winfo_height())
        self.assertGreaterEqual(app.log.winfo_height(), 140)
        app.root.geometry("980x820+20000+20000"); app.root.update(); app.root.update_idletasks()
        self.assertTrue(app.group_scrollbar.winfo_ismapped())
        app.group_tree.yview_moveto(1); app.root.update_idletasks()
        last = app.group_tree.bbox("9")
        self.assertTrue(last)
        self.assertLessEqual(last[1] + last[3], app.group_tree.winfo_height())

    def test_bound_review_options_are_explained_before_start(self):
        app = self.app()
        options = {"review_enabled": True, "legacy": True, "review_profile": {
            "model": "gpt-6.1-sol", "locator_effort": "xhigh", "body_effort": "xhigh"}}
        with patch("soutui_image.desktop_app.read_options", return_value=options):
            app.show_selected_product()
        self.assertFalse(app.review_enabled.get())
        for text in ("沿用原批次", "开启审图", "Sol", "xhigh", "这些选项用于新任务"):
            self.assertIn(text, app.quote_text.get())
        self.assertFalse(app.busy)

    def test_pending_images_show_saved_names_and_reasons_read_only(self):
        app = self.app()
        record = {"groups": [{"row": 3, "directory": "组03", "saved": [
            {"filename": "待确认.jpg", "review_status": "review", "summary": "衣领被遮挡"},
            {"filename": "未完成.jpg", "review_status": "error", "summary": "审核请求未完成"},
            {"filename": "通过.jpg", "review_status": "pass", "summary": "通过"}]}]}
        files_before = sorted(str(p) for p in self.base.rglob("*"))
        with patch("soutui_image.desktop_app.read_manifest", return_value=record), \
             patch("soutui_image.desktop_app.atomic_write_json", side_effect=AssertionError("只读入口不能写入")), \
             patch("soutui_image.desktop_app.Pipeline", side_effect=AssertionError("只读入口不能审图")):
            app.show_pending_images()
        text = app.pending_text.get("1.0", "end")
        for expected in ("组03", "待确认.jpg", "未完成.jpg", "衣领被遮挡", "审核请求未完成"):
            self.assertIn(expected, text)
        self.assertNotIn("通过.jpg", text)
        self.assertEqual(str(app.pending_text.cget("state")), "disabled")
        self.assertEqual(files_before, sorted(str(p) for p in self.base.rglob("*")))

    def test_new_action_layout_large_checkmark_and_model_dropdown(self):
        app = self.app()
        app.root.geometry("1140x850+20000+20000"); app.root.deiconify(); app.root.update(); app.root.update_idletasks()
        self.assertFalse(hasattr(app, "options_row"))
        self.assertEqual(app.review_check.master, app.start_button.master)
        self.assertEqual(app.recheck_button.master, app.start_button.master)
        self.assertLess(app.start_button.winfo_x(), app.review_check.winfo_x())
        self.assertLess(app.review_check.winfo_x(), app.model_button.winfo_x())
        self.assertLess(app.model_button.winfo_x(), app.recheck_button.winfo_x())
        self.assertGreater(app.resolution_button.winfo_rooty(), app.start_button.winfo_rooty())
        self.assertEqual(app.review_indicator_images[0].width(), 26)
        app.review_check.invoke()
        self.assertTrue(app.review_enabled.get())
        self.assertIn("selected", app.review_check.state())
        app.model_button.current(1); app._model_selected()
        self.assertEqual(app.review_model.get(), "gpt-6.1-terra")

    def test_regenerate_ignores_review_checkbox_and_does_not_require_codex(self):
        app = self.app(); app.connected = True; app.codex_ready = False
        app.review_enabled.set(True); self.jobs.clear()
        with patch("soutui_image.desktop_app.Pipeline") as factory:
            factory.return_value.regenerate.return_value = {"status": "complete"}
            app.regenerate(); self.assertTrue(app.busy)
            self.assertEqual(str(app.regenerate_button.cget("state")), "disabled")
            self.jobs.pop()()
            factory.return_value.regenerate.assert_called_once_with(str(self.inputs), product_ids=("001",))
            factory.return_value.run.assert_not_called()
            factory.return_value.recheck_saved.assert_not_called()

    def test_regenerate_pending_connection_resumes_the_same_action(self):
        app = self.app(); self.jobs.clear()
        with patch("soutui_image.desktop_app.Pipeline") as factory:
            app.regenerate()
            self.assertTrue(app.pending_start)
            app.connected = True; app.events.put({"kind": "connection_finished"}); app._drain()
            self.assertTrue(app.busy)
            factory.return_value.regenerate.return_value = {"status": "complete"}
            self.jobs[-1]()
            factory.return_value.regenerate.assert_called_once()
            factory.return_value.run.assert_not_called()


if __name__ == "__main__": unittest.main()
