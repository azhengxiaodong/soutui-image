"""审核调度：独立整图复核和扣位专项并行，保留全部检查、规则上下文及失败保护。"""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from time import perf_counter


def run_independent_checks(body, buttons=None, *, parallel=True):
    """运行互不读取对方结论的两项检查；任一失败不产生通过结果，并返回实际耗时。"""
    timing = {"mode": "parallel" if parallel and buttons is not None else "sequential"}
    start = perf_counter()

    def timed(name, callback):
        timing[name + "_started"] = perf_counter() - start
        try:
            return callback()
        finally:
            timing[name + "_finished"] = perf_counter() - start

    if parallel and buttons is not None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            body_future = executor.submit(copy_context().run, timed, "body", body)
            button_future = executor.submit(copy_context().run, timed, "buttons", buttons)
            verdict, check = body_future.result(), button_future.result()
    else:
        verdict = timed("body", body)
        check = timed("buttons", buttons) if buttons is not None else None
    timing["total_seconds"] = perf_counter() - start
    return verdict, check, timing
