"""实际创建桌面窗口并截取本软件画面；不点击生成、不连接新账号。"""

import argparse
import json
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import ImageGrab
from soutui_image.desktop_app import Workbench


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    root = tk.Tk()
    app = Workbench(root, args.data_dir, args.input)
    target = ROOT / "validation" / "gui"
    target.mkdir(parents=True, exist_ok=True)
    def capture():
        root.lift(); root.focus_force(); root.update_idletasks()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        width, height = root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True).save(target / "preview.png")
        result = {"window_created": True, "size": [width, height], "products": len(app.products),
                  "group_rows": len(app.group_tree.get_children()), "connected": app.connected,
                  "codex_ready": app.codex_ready, "generation_started": app.busy}
        (target / "smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True), flush=True)
        root.after(500, root.destroy)
    root.after(4000, capture)
    root.mainloop()


if __name__ == "__main__":
    main()
