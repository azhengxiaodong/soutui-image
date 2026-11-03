"""H 独立回归：真实历史副本，通过界面复核、暂停和继续；禁止新增模型和网站调用。"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.desktop_app import Workbench
from soutui_image.stage_e_codex_review import POLICY_VERSION
from soutui_image.window_capture import capture_own_window


def read(path): return json.loads(path.read_text(encoding="utf-8"))
def snapshot(paths): return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    """复用五份已完成的真实 Sol 结论，不给程序注入虚构审核或网站结果。"""
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); target = args.output.resolve()
    subprocess.run([sys.executable, str(ROOT / "tools/validate_quality_prefix.py"), "--output", str(target), "--prepare-only"], check=True)
    pid = read(target / "prepared.json")["product_id"]
    data, inputs = target / "data", target / "input"
    tasks = data / "tasks" / pid
    before = snapshot(tasks.glob("group_*/round_*.json"))
    errors, phases = [], []
    root = tk.Tk(); app = Workbench(root, data, inputs)
    started = time.monotonic(); phase = "ready"
    def forbidden(*args, **kwargs): raise AssertionError("复核验收禁止调用模型或网站生成")
    def tick():
        nonlocal phase
        try:
            if time.monotonic() - started > 90: raise TimeoutError("复核界面验收超时")
            if phase == "ready" and app.codex_ready:
                # 测试不依赖网站已连接；正常检查环境已真实完成 Codex 登录检查。
                assert not app.connected
                app.product_tree.selection_set(pid); app.show_selected_product()
                assert sum(app._saved_counts(pid).values()) == 0
                assert app._pending_rows(pid) == {1}
                assert app.group_tree.item("1", "values")[2] == "旧图待复核"
                app.recheck_button.invoke(); phase = "pausing"; root.after(5, app.pause)
            elif phase == "pausing" and not app.busy:
                result = read(data / "last_run_result.json"); assert result["status"] == "paused", result
                phases.append("paused"); app.recheck_button.invoke(); phase = "resuming"
            elif phase == "resuming" and not app.busy:
                result = read(data / "last_run_result.json"); assert result["status"] == "complete", result
                assert len(result["products"]) == 1
                assert before == snapshot(tasks.glob("group_*/round_*.json"))
                assert sum(app._saved_counts(pid).values()) == 1
                assert app._pending_rows(pid) == set()
                assert int(app.product_tree.item(pid, "values")[2]) == 1
                assert app.group_tree.item("1", "values")[2] == "待补生"
                phases.append("complete")
                manifest = read(inputs / pid / ".搜推生图.json")
                assert len(manifest["groups"]) == 9
                for entry in manifest["withdrawn"]:
                    assert hashlib.sha256(Path(entry["retained_path"]).read_bytes()).hexdigest() == entry["sha256"]
                root.update_idletasks()
                capture_own_window(root, target / "preview.png")
                report = {"status":"verified", "actual_gui_recheck_pause_resume":True, "website_connected":False,
                          "results":phases, "current_saved":1, "retained_old_images":len(manifest["withdrawn"]),
                          "policy_version":POLICY_VERSION, "review_model":"gpt-6.1-sol", "job_files_unchanged":True,
                          "new_generation_submissions":0, "new_model_calls":0, "nine_group_directories":True,
                          "scope":"真实首组两轮前缀副本及五张当前审核缓存，非整35张视觉复核"}
                (target / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=True), flush=True); root.destroy(); return
            root.after(50, tick)
        except Exception as error:
            errors.append(str(error)); app.stop.set()
            print(json.dumps({"status":"failed", "error":str(error)}, ensure_ascii=True), flush=True); root.destroy()
    with patch("soutui_image.stage_e_codex_review._audit_once", side_effect=forbidden), patch("soutui_image.pipeline.WebTransport", side_effect=forbidden):
        root.after(100, tick); root.mainloop()
    return 2 if errors else 0


if __name__ == "__main__": raise SystemExit(main())
