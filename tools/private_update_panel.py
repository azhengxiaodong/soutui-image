"""私有更新独立桌面验证入口；不接入旧主程序，不生成或审核任何商品图。"""

import argparse
import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from soutui_image.update_service import UpdateService
from soutui_image.update_dialog import show_access_dialog
from soutui_image.window_capture import capture_own_window


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--data-dir",type=Path,required=True)
    parser.add_argument("--smoke-output",type=Path); args=parser.parse_args()
    service=UpdateService(args.data_dir); events=queue.Queue(); root=tk.Tk()
    root.title("搜推生图 · 私有更新模块"); root.geometry("650x260")
    main=ttk.Frame(root,padding=20); main.pack(fill="both",expand=True)
    ttk.Label(main,text="私有审图规则更新",font=("Microsoft YaHei UI",16,"bold")).pack(anchor="w")
    ttk.Label(main,text=service.source["owner"]+"/"+service.source["repository"]).pack(anchor="w",pady=10)
    status=tk.StringVar(value="正在检查已发布规则…"); ttk.Label(main,textvariable=status,wraplength=600).pack(anchor="w",pady=10)
    def done(result): events.put(result)
    def check():
        def worker():
            try: done(service.check())
            except Exception as error: done({"status":"error","message":str(error)})
        threading.Thread(target=worker,daemon=True).start()
    controls=ttk.Frame(main); controls.pack(fill="x",pady=10)
    ttk.Button(controls,text="检查规则更新",command=check).pack(side="left")
    ttk.Button(controls,text="配置本机只读授权",command=lambda:show_access_dialog(root,service,done)).pack(side="left",padx=10)
    def tick():
        try:
            while True:
                result=events.get_nowait(); status.set(result["message"])
                if args.smoke_output:
                    args.smoke_output.mkdir(parents=True,exist_ok=True); root.update_idletasks()
                    capture_own_window(root,args.smoke_output/"preview.png")
                    (args.smoke_output/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
                    root.after(500,root.destroy); return
        except queue.Empty: pass
        root.after(100,tick)
    check(); root.after(100,tick); root.mainloop()


if __name__=="__main__": main()
