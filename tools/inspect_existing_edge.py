"""只读观察现有 Edge 的可访问控件，不启动、不关闭或切换浏览器窗口。"""

import argparse
import json
from pathlib import Path

import uiautomation as auto


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    candidates = [control for control in auto.GetRootControl().GetChildren() if control.ProcessId == args.pid]
    windows = [{"title": c.Name, "handle": c.NativeWindowHandle, "class": c.ClassName} for c in candidates]
    print(json.dumps({"windows": windows}, ensure_ascii=True), flush=True)
    if len(candidates) != 1:
        raise SystemExit("需要唯一现有 Edge 窗口，未操作任何窗口")
    controls = []

    def walk(control, depth=0):
        if len(controls) >= 2500 or depth > 25:
            return
        controls.append({"depth": depth, "name": control.Name, "type": control.ControlTypeName, "automation_id": control.AutomationId})
        for child in control.GetChildren():
            walk(child, depth + 1)

    # 只展开目标网站文档，避免采集其他标签页内容和浏览器工具栏。
    documents = []

    def find_document(control, depth=0):
        if depth > 14:
            return
        if control.ControlTypeName == "DocumentControl" and "MuseFit AI" in control.Name:
            documents.append(control)
            return
        for child in control.GetChildren():
            find_document(child, depth + 1)

    find_document(candidates[0])
    if len(documents) != 1:
        raise SystemExit("无法唯一定位生图网站文档，未操作浏览器")
    walk(documents[0])
    target = Path(__file__).resolve().parents[1] / "validation" / "stage_b" / "existing_edge.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"windows": windows, "controls": controls}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([c for c in controls if c["name"]], ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
