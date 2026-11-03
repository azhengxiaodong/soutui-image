"""私有更新授权窗口：令牌只进本机加密存储，操作结果经队列回到主界面。"""

import threading
import tkinter as tk
from tkinter import ttk,messagebox


def show_access_dialog(root,service,on_result):
    """输入当前电脑专用的仓库Contents只读授权；后台验证，窗口不显示明文。"""
    dialog=tk.Toplevel(root); dialog.title("配置私有更新授权"); dialog.geometry("580x235"); dialog.transient(root)
    main=ttk.Frame(dialog,padding=18); main.pack(fill="both",expand=True)
    ttk.Label(main,text="GitHub 私有更新仓库",font=("Microsoft YaHei UI",12,"bold")).pack(anchor="w")
    ttk.Label(main,text=service.source["owner"]+"/"+service.source["repository"]).pack(anchor="w",pady=4)
    ttk.Label(main,text="输入仅此仓库 Contents: Read 的细粒度令牌，保存在当前电脑。",wraplength=540).pack(anchor="w",pady=5)
    token=tk.StringVar(); entry=ttk.Entry(main,textvariable=token,show="●"); entry.pack(fill="x",pady=5)
    status=tk.StringVar(); ttk.Label(main,textvariable=status,wraplength=540).pack(anchor="w")
    state={"result":None,"error":None,"ready":False}
    def submit():
        value=token.get().strip(); token.set(""); button.configure(state="disabled"); status.set("正在验证私有仓库访问…")
        def worker():
            try: state["result"]=service.configure_read_access(value)
            except Exception as error: state["error"]=str(error)
            finally: state["ready"]=True
        threading.Thread(target=worker,daemon=True).start(); dialog.after(100,poll)
    def poll():
        if not dialog.winfo_exists(): return
        if not state["ready"]: dialog.after(100,poll); return
        if state["error"]:
            status.set(state["error"]); button.configure(state="normal"); state.update(result=None,error=None,ready=False)
        else:
            on_result(state["result"]); dialog.destroy()
    button=ttk.Button(main,text="验证并保存",command=submit); button.pack(anchor="e",pady=8)
    entry.focus_set()
