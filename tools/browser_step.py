"""板块 B 实际页面逐步验证入口，每次明确执行一个已观察到的动作并记录结果。"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soutui_image.browser_ui import ExistingBrowser


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--click")
    parser.add_argument("--select-tab", action="store_true")
    parser.add_argument("--file-path")
    parser.add_argument("--confirm-file", action="store_true")
    parser.add_argument("--role")
    parser.add_argument("--prefix", action="store_true")
    parser.add_argument("--occurrence", type=int)
    parser.add_argument("--record", required=True)
    args = parser.parse_args()
    if re.fullmatch(r"[a-zA-Z0-9_-]+", args.record) is None:
        raise ValueError("验证记录名只能包含字母、数字、下划线和横线")
    browser = ExistingBrowser()
    if sum(bool(v) for v in (args.select_tab, args.click, args.file_path, args.confirm_file)) > 1:
        raise ValueError("每次只验证一个动作")
    before = [] if args.select_tab else browser.observe()
    if args.select_tab:
        browser.select_target_tab()
        time.sleep(1.2)
    elif args.file_path:
        browser.set_file_dialog_path(args.file_path)
    elif args.confirm_file:
        browser.confirm_file_dialog()
        time.sleep(2)
    elif args.click:
        browser.click_name(args.click, role=args.role, prefix=args.prefix, occurrence=args.occurrence)
        time.sleep(1.2)
    after = browser.observe()
    report = {"action": "select_target_tab" if args.select_tab else args.click or args.file_path or ("confirm_file" if args.confirm_file else "observe"), "before": before, "after": after}
    target = ROOT / "validation" / "stage_b" / f"{args.record}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"changed": before != after, "controls": [c for c in after if c["type"] in ("ButtonControl", "ComboBoxControl", "EditControl", "RadioButtonControl") and not c["offscreen"]], "text": [c["name"] for c in after if c["type"] == "TextControl" and not c["offscreen"]][:45]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
