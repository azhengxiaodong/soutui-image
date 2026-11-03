"""首次连接助手：复用唯一的现有 MuseFit 标签，连接后 HTTP 业务不再依赖界面。"""

import time


def connect_existing_session(session_path):
    import uiautomation as auto
    from .browser_ui import ExistingBrowser
    from tools.connect_existing_browser import _check_site
    from .session_bridge import connect_browser
    with auto.UIAutomationInitializerInThread():
        browser = ExistingBrowser()
        window = browser.window()
        window.SetActive()
        tabs = [x.control for x in browser.walk(window, max_depth=14)
                if x.control.ControlTypeName == "TabItemControl" and x.control.Name.startswith("MuseFit AI")]
        if len(tabs) != 1:
            raise RuntimeError("请在已有浏览器保留一个已登录的 MuseFit 生图标签")
        if not tabs[0].GetSelectionItemPattern().IsSelected:
            tabs[0].Click(simulateMove=False, waitTime=.1)
        deadline = time.monotonic() + 10
        while True:
            try: _check_site(browser); break
            except RuntimeError:
                if time.monotonic() >= deadline: raise
                time.sleep(.25)
        devs = [x.control for x in browser.walk(window, max_depth=16)
                if x.control.ControlTypeName == "DocumentControl" and x.control.Name == "DevTools"]
        opened = not devs
        if opened:
            window.SendKeys("{F12}", waitTime=.1)
            time.sleep(1)
        try:
            return connect_browser(browser, session_path)
        finally:
            if opened:
                _check_site(browser)
                window.SendKeys("{F12}", waitTime=.1)
