"""U_E_RUNTIME：每个商品使用不可变规则上下文；并行线程显式继承，避免跨版本串用。"""

from contextlib import contextmanager
from contextvars import ContextVar

from .rules_bundle import builtin_bundle
from .rule_store import policy_id


_active = ContextVar("soutui_active_rules",default=None)
_builtin = None


def active_rules():
    global _builtin
    chosen=_active.get()
    if chosen is not None: return chosen
    if _builtin is None: _builtin=builtin_bundle()
    return _builtin


def current_policy():
    """当前审核记录的实际版本身份，新版内容哈希参与缓存隔离。"""
    return policy_id(active_rules())


@contextmanager
def use_rules(bundle):
    """只在明确任务入口激活；离开商品任务后恢复先前上下文，不修改全局模板。"""
    policy_id(bundle)
    token=_active.set(bundle)
    try: yield bundle
    finally: _active.reset(token)
