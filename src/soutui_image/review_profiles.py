"""审图执行配置：模型与各阶段推理强度随任务固定，并参与所有审核缓存身份。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass


MODELS = ("gpt-6.1-sol", "gpt-6.1-terra")
SUPPORTED_MODELS = MODELS
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ReviewProfile:
    """不可变的逐阶段配置；legacy 只用于保留原 Sol/xhigh 任务和断点。"""

    model: str
    locator_effort: str = "medium"
    body_effort: str = "high"
    button_effort: str = "high"
    legacy: bool = False

    def __post_init__(self):
        if self.model not in MODELS:
            raise ValueError("审图模型仅支持 GPT-6.1 Sol 或 GPT-6.1 Terra")
        if any(value not in EFFORTS for value in
               (self.locator_effort, self.body_effort, self.button_effort)):
            raise ValueError("审图推理强度无效")
        if type(self.legacy) is not bool:
            raise ValueError("旧任务标记必须为布尔值")
        if self.legacy and (self.model != MODELS[0] or
                            {self.locator_effort, self.body_effort, self.button_effort} != {"xhigh"}):
            raise ValueError("旧审核任务必须沿用 Sol/xhigh，不能混入新模型或推理强度")

    def to_dict(self):
        """输出可存入任务记录的完整配置，不依赖用户全局配置的后续变化。"""
        return {"schema_version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        """严格读取已固定配置，缺失或未知字段都不能被默认值静默补齐。"""
        fields = {"schema_version", "model", "locator_effort", "body_effort", "button_effort", "legacy"}
        if (not isinstance(value, dict) or set(value) != fields or
                type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError("已保存的审图配置格式无效")
        return cls(**{key: value[key] for key in fields - {"schema_version"}})


def profile(model=MODELS[0]):
    """新审核候选：定位 medium，首审/独立复核/扣位专项 high。"""
    return ReviewProfile(model)


def legacy_profile():
    """旧业务保留原 Sol/xhigh；其已成功缓存仍使用原有目录身份。"""
    return ReviewProfile(MODELS[0], "xhigh", "xhigh", "xhigh", legacy=True)


_active = ContextVar("soutui_review_profile", default=None)


def current_profile(model=None):
    """返回当前任务配置；显式 model 必须和任务上下文相同。"""
    chosen = _active.get()
    if chosen is None:
        chosen = legacy_profile() if model in (None, MODELS[0]) else profile(model)
    if model is not None and model != chosen.model:
        raise ValueError("传入审图模型与当前任务固定配置不一致")
    return chosen


@contextmanager
def use_review_profile(chosen):
    """临时激活任务配置；线程使用 copy_context 继承，结束后恢复原上下文。"""
    if not isinstance(chosen, ReviewProfile):
        raise TypeError("审图上下文必须使用已校验的 ReviewProfile")
    token = _active.set(chosen)
    try:
        yield chosen
    finally:
        _active.reset(token)


def profile_fingerprint(chosen=None):
    """稳定摘要用于隔离不同模型、推理强度及未来配置版本的审核证据。"""
    chosen = chosen or current_profile()
    value = json.dumps(chosen.to_dict(), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def review_cache_key(product, candidate, *, model=None, policy_version=None):
    """同图同规则同配置才复用；旧 Sol/xhigh 的 key 保持逐字节兼容。"""
    from .rule_runtime import current_policy
    chosen = current_profile(model)
    get = (lambda key: candidate[key]) if isinstance(candidate, dict) else (lambda key: getattr(candidate, key))
    parts = [policy_version or current_policy(), product.product_id, get("result_id"),
             product.sha256, get("sha256"), chosen.model]
    if not chosen.legacy:
        parts.append("review-profile:" + profile_fingerprint(chosen))
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def validate_review_profile(review, *, model=None):
    """轮次、最终缓存等复用入口共用此检查；缺配置的历史记录只属于旧任务。"""
    chosen = current_profile(model)
    value = review.get("review_profile") if isinstance(review, dict) else review.review_profile
    if value is None:
        if not chosen.legacy:
            raise ValueError("旧审核未记录当前模型和推理配置，不能作为本次审核断点")
    elif ReviewProfile.from_dict(value) != chosen:
        raise ValueError("审核记录属于其他模型或推理强度，不能复用")
    return chosen


def phase_profile_identity(*, model=None):
    """新阶段断点明确包含执行配置；旧阶段保持原格式，仍由旧任务上下文限定。"""
    chosen = current_profile(model)
    return {} if chosen.legacy else {"review_profile": chosen.to_dict()}


def reasoning_arguments(stage, *, model=None):
    """仅设置当前 CLI 子进程的推理强度，不覆盖 HTTPS、认证、端点和原图细节。"""
    chosen = current_profile(model)
    values = {"locator": chosen.locator_effort, "body": chosen.body_effort, "buttons": chosen.button_effort}
    if stage not in values:
        raise ValueError("未知审核阶段")
    return ["-c", 'model_reasoning_effort="' + values[stage] + '"']
