"""在已处理完的真实商品上验证 GUI 开始、暂停、继续；禁止拿未完成商品做免费重跑测试。"""

import argparse
import hashlib
import json
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.desktop_app import Workbench
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_c_web_plan import load_plan
from soutui_image.stage_d_web_generation import group_decision
from soutui_image.window_capture import capture_own_window


def snapshot(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--product-id", required=True)
    args = parser.parse_args()
    data = ROOT / "data"
    product = next(p for p in scan_inputs(args.input).products if p.product_id == args.product_id)
    plan = load_plan(data / "tasks" / f"product_{product.product_id}_references.json")
    states = [group_decision(data / "tasks", product, g).action for g in plan.assignments]
    if any(state not in ("complete", "paused") for state in states):
        raise RuntimeError("商品尚未处理结束，不能执行 GUI 无新生成续跑验收")
    target = ROOT / "validation" / "gui_resume"
    target.mkdir(parents=True, exist_ok=True)
    tasks = data / "tasks" / product.product_id
    before_jobs = snapshot(tasks.glob("group_*/round_*.json"))
    pictures = Path(product.source_path).parent / product.product_id
    before_images = snapshot(pictures.glob("组*/*.jpg"))
    event_path = data / "last_run.events.jsonl"
    offset = event_path.stat().st_size if event_path.exists() else 0
    root = tk.Tk()
    app = Workbench(root, data, args.input)
    started = time.monotonic()
    phase, outcomes = "ready", []
    errors = []
    def tick():
        nonlocal phase
        try:
            if time.monotonic() - started > 60: raise TimeoutError("界面暂停/继续验收超时")
            if phase == "ready" and app.connected and app.codex_ready:
                app.product_tree.selection_set(product.product_id)
                app.show_selected_product()
                app.start(); phase = "pausing"
                root.after(5, app.pause)
            elif phase == "pausing" and not app.busy:
                result = json.loads((data / "last_run_result.json").read_text(encoding="utf-8"))
                assert result["status"] == "paused", "未实际验证到暂停"
                outcomes.append(result["status"])
                app.start(); phase = "resuming"
            elif phase == "resuming" and not app.busy:
                result = json.loads((data / "last_run_result.json").read_text(encoding="utf-8"))
                assert result["status"] in ("complete", "needs_attention")
                assert len(result["products"]) == 1 and len(result["products"][0]["groups"]) == 9
                outcomes.append(result["status"])
                assert before_jobs == snapshot(tasks.glob("group_*/round_*.json"))
                assert before_images == snapshot(pictures.glob("组*/*.jpg"))
                with event_path.open("rb") as handle:
                    handle.seek(offset)
                    events = [json.loads(line) for line in handle.read().decode("utf-8").splitlines()]
                assert not any(e["kind"] == "submitting" for e in events), "重复开始不应提交生成"
                capture_own_window(root, target / "preview.png")
                report = {"actual_gui_start_pause_resume": True, "product_id": product.product_id,
                          "results": outcomes, "groups": result["products"][0]["groups"],
                          "new_generation_submissions": 0, "job_files_unchanged": True,
                          "approved_images_unchanged": True, "selected_product_only": True}
                (target / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=True), flush=True)
                root.destroy(); return
            root.after(100, tick)
        except Exception as error:
            errors.append(str(error)); app.stop.set()
            print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=True), flush=True)
            root.destroy()
    root.after(100, tick)
    root.mainloop()
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
