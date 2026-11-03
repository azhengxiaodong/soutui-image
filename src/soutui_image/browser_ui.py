"""现有浏览器控制：通过 Windows 可访问控件复用当前 MuseFit 窗口，不启动浏览器。

窗口在每次操作时重新定位；定位不唯一、页面不匹配或元素歧义时停止，不猜坐标。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import uiautomation as auto


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class ObservedControl:
    control: object
    depth: int

    def describe(self):
        result = {"depth": self.depth, "name": self.control.Name, "type": self.control.ControlTypeName,
                "automation_id": self.control.AutomationId, "offscreen": self.control.IsOffscreen}
        if self.control.ControlTypeName == "EditControl":
            pattern = self.control.GetValuePattern()
            result["value"] = pattern.Value if pattern else None
        return result


class ExistingBrowser:
    """只使用标题含 MuseFit AI 的唯一现有浏览器窗口；不处理用户登录。"""

    def window(self):
        candidates = [c for c in auto.GetRootControl().GetChildren()
                      if c.ClassName == "Chrome_WidgetWin_1" and ("Edge" in c.Name or "Chrome" in c.Name)]
        candidates = [window for window in candidates if "MuseFit AI" in window.Name or any(
            item.control.ControlTypeName == "TabItemControl" and item.control.Name.startswith("MuseFit AI")
            for item in self.walk(window, max_depth=12))]
        if len(candidates) != 1:
            raise RuntimeError("请在现有浏览器中选中 MuseFit 生图页；需要唯一的目标窗口")
        return candidates[0]

    def select_target_tab(self):
        """只切换到已存在的 MuseFit 标签，不创建新标签页或新窗口。"""
        window = self.window()
        tabs = [c.control for c in self.walk(window, max_depth=12)
                if c.control.ControlTypeName == "TabItemControl" and c.control.Name.startswith("MuseFit AI")]
        if len(tabs) != 1:
            raise RuntimeError("需要唯一的已存在 MuseFit 标签页")
        pattern = tabs[0].GetSelectionItemPattern()
        if not pattern:
            raise RuntimeError("该标签页不支持可靠的语义切换，请手动选择生图页")
        pattern.Select()

    @staticmethod
    def walk(control, max_depth=28):
        """有界展开当前控件树，避免无界遍历其他页面。"""
        collected = []

        def visit(current, depth):
            if depth > max_depth or len(collected) >= 4000:
                return
            collected.append(ObservedControl(current, depth))
            for child in current.GetChildren():
                visit(child, depth + 1)

        visit(control, 0)
        return collected

    def document(self):
        found = [c.control for c in self.walk(self.window(), max_depth=14)
                 if c.control.ControlTypeName == "DocumentControl" and "MuseFit AI" in c.control.Name]
        if len(found) != 1:
            raise RuntimeError("无法唯一定位当前生图网页，已停止操作")
        legacy = found[0].GetLegacyIAccessiblePattern()
        if not legacy or not legacy.Value.startswith("https://ai.51dingxiao.com/app/"):
            raise RuntimeError("当前网页地址不是已确认的生图网站，已停止操作")
        return found[0]

    def observe(self):
        """观察当前目标网页；返回的数据不包含其他标签页和浏览器密码等内容。"""
        return [c.describe() for c in self.walk(self.document())
                if c.control.Name or c.control.ControlTypeName == "EditControl"]

    def wait_for(self, predicate, description, timeout=30):
        """按页面状态等待；超时后报告当前状态，不盲目重复上传或点击。"""
        deadline = time.monotonic() + timeout
        last = []
        while True:
            last = self.observe()
            if predicate(last):
                return last
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待{description}超时，已停止；请查看验证记录")
            time.sleep(0.35)

    def wait_file_dialog(self, timeout=10):
        """等待浏览器自己的文件选择框出现，限定时间内不重复点击上传。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.file_dialog()
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待打开文件对话框超时") from None
                time.sleep(0.25)

    def rename_project(self, product_id):
        """在已创建项目的标题输入框填写商品 ID，回车后核验标题。"""
        if re.fullmatch(r"[0-9]+", product_id) is None:
            raise ValueError("项目名称必须是商品数字 ID")
        self.click_name("重命名项目", role="ButtonControl")
        self.wait_for(lambda rows: any(c["type"] == "EditControl" and not c["name"]
                                      and not c["offscreen"] for c in rows), "项目名称输入框")
        fields = [c.control for c in self.walk(self.document())
                  if c.control.ControlTypeName == "EditControl" and not c.control.Name
                  and not c.control.IsOffscreen]
        if len(fields) != 1:
            raise RuntimeError("项目名称输入框不唯一，停止重命名")
        field = fields[0]
        field.GetValuePattern().SetValue(product_id)
        if field.GetValuePattern().Value != product_id:
            raise RuntimeError("项目名称写入失败")
        field.SetFocus()
        if not field.HasKeyboardFocus:
            raise RuntimeError("项目名称框未获得焦点，停止提交")
        field.SendKeys("{Enter}", waitTime=0.1)
        return self.wait_for(lambda rows: any(c["name"] == f"模特换衣｜{product_id}"
                                              for c in rows), "商品项目名称保存")

    def file_dialog(self):
        """只定位当前目标浏览器拥有的文件对话框，不操作其他软件的窗口。"""
        dialogs = [c for c in self.window().GetChildren()
                   if c.ControlTypeName == "WindowControl" and c.Name in ("打开", "Open")]
        if len(dialogs) != 1:
            raise RuntimeError("当前目标浏览器没有唯一的打开文件对话框")
        return dialogs[0]

    def set_file_dialog_path(self, path):
        """使用文件名输入框的值接口填入原图完整路径，不使用剪贴板或终端命令。"""
        source = Path(path).resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise ValueError("请选择已验证的商品原图文件")
        fields = [c.control for c in self.walk(self.file_dialog(), max_depth=8)
                  if c.control.ControlTypeName == "EditControl" and c.control.AutomationId == "1148"]
        if len(fields) != 1:
            raise RuntimeError("无法唯一定位文件名输入框")
        value = fields[0].GetValuePattern()
        value.SetValue(str(source))
        if value.Value != str(source):
            raise RuntimeError("文件名未能正确写入，停止上传")

    def confirm_file_dialog(self):
        """只调用目标文件对话框里已观察到的标准打开按钮。"""
        buttons = [c.control for c in self.walk(self.file_dialog(), max_depth=4)
                   if c.control.ControlTypeName == "ButtonControl" and c.control.AutomationId == "1"]
        if len(buttons) != 1:
            raise RuntimeError("无法唯一定位打开按钮")
        buttons[0].GetInvokePattern().Invoke()

    def click_name(self, name, *, role=None, prefix=False, occurrence=None):
        """对新观察到的明确控件执行一次点击。每次调用只做一次业务动作。"""
        self.window().SetActive()
        candidates = []
        for item in self.walk(self.document()):
            value = normalized(item.control.Name)
            match = value.startswith(normalized(name)) if prefix else value == normalized(name)
            if match and not item.control.IsOffscreen and (role is None or item.control.ControlTypeName == role):
                candidates.append(item)
        if role is None and candidates:
            # 同一按钮常同时暴露按钮与文本控件，优先使用按钮语义。
            for preferred in ("ButtonControl", "HyperlinkControl", "TextControl"):
                subset = [c for c in candidates if c.control.ControlTypeName == preferred]
                if subset:
                    candidates = subset
                    break
            shallowest = min(c.depth for c in candidates)
            candidates = [c for c in candidates if c.depth == shallowest]
        if occurrence is None:
            if len(candidates) != 1:
                raise RuntimeError(f"控件 {name!r} 匹配到 {len(candidates)} 个，已停止以免误操作")
            chosen = candidates[0].control
        else:
            if occurrence < 0 or occurrence >= len(candidates):
                raise RuntimeError("指定控件序号不在当前观察结果中")
            chosen = candidates[occurrence].control
        invoke = getattr(chosen, "GetInvokePattern", lambda: None)()
        if invoke:
            invoke.Invoke()
        else:
            raise RuntimeError("目标控件未提供语义点击，暂不使用未验证的坐标点击")

