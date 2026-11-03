"""审核记录版本兼容：旧拒绝可继续保留为拒绝，旧通过必须按新规则重审。"""

from .stage_e_codex_review import (LEGACY_FEATURES, REVIEW_SCHEMA, CHECK_SCHEMA, VISIBILITY, COMPARISONS, validate_review)
from .rule_runtime import current_policy


LEGACY_POLICY = "2026-10-27.1"


def _legacy_nonpass(verdict, product_id, result_id, *, allow_initial_pass=False):
    """只检查旧十项证据自身有效性；不补写新检查项，也不把旧记录改成新版审核。"""
    if (not isinstance(verdict, dict) or set(verdict) != set(REVIEW_SCHEMA["required"])
            or verdict["product_id"] != product_id or verdict["result_id"] != result_id):
        raise ValueError("旧审核对象或字段不完整")
    checks = verdict["checks"]
    if not isinstance(checks, list) or len(checks) != len(LEGACY_FEATURES):
        raise ValueError("旧审核没有完整十项证据")
    if {c.get("feature") for c in checks} != set(LEGACY_FEATURES):
        raise ValueError("旧审核检查项缺失或重复")
    visible = []
    for check in checks:
        if (not isinstance(check, dict) or set(check) != set(CHECK_SCHEMA["required"])
                or check["visibility"] not in VISIBILITY or check["comparison"] not in COMPARISONS):
            raise ValueError("旧检查项格式错误")
        if not all(isinstance(check[k], str) and check[k].strip() for k in ("original", "generated", "reason")):
            raise ValueError("旧检查项缺少证据")
        if (check["visibility"] == "not_visible") != (check["comparison"] == "not_applicable"):
            raise ValueError("旧检查项可见性与结论矛盾")
        if check["visibility"] != "not_visible":
            visible.append(check)
    expected = "reject" if any(c["comparison"] == "mismatch" for c in visible) else "review" if any(c["comparison"] == "uncertain" for c in visible) else "pass"
    if not visible or (expected == "pass" and not allow_initial_pass) or verdict["decision"] != expected:
        raise ValueError("旧证据只能保留有效的非通过结论，不能直接算新版合格")
    if (not all(isinstance(verdict[k], str) and verdict[k].strip() for k in ("visible_scope", "summary"))
            or not isinstance(verdict["unshown_parts"], list) or not all(isinstance(x, str) for x in verdict["unshown_parts"])):
        raise ValueError("旧结论缺少范围说明")


def validate_bound_review(product, candidate, review):
    """输入绑定的商品、结果和存档；返回有效结论，任何旧 pass 都会拒绝消费。"""
    get = (lambda key: candidate[key]) if isinstance(candidate, dict) else (lambda key: getattr(candidate, key))
    if (get("product_id") != product.product_id or review["product_id"] != product.product_id
            or review["source_sha256"] != product.sha256 or review["result_id"] != get("result_id")
            or review["result_sha256"] != get("sha256")):
        raise ValueError("审核记录与当前原图/结果身份不符")
    current = review["policy_version"] == current_policy()
    old_fourteen = (review["policy_version"] == "2026-10-29.5" or
                    isinstance(review["policy_version"],str) and "@" in review["policy_version"])
    if not current and (review["policy_version"] != LEGACY_POLICY and not old_fourteen or review["decision"] == "pass"):
        raise ValueError("旧规则通过必须重新审核；未知版本不能作为当前审核证据")
    passes = review["passes"]
    if not isinstance(passes, (list, tuple)) or not passes:
        raise ValueError("缺少逐张真实审核证据")
    for verdict in passes:
        if current or old_fourteen:
            validate_review(verdict, product.product_id, get("result_id"))
        else:
            # 旧记录可能初审拟通过、第二次才拒绝；初审仍按十项完整格式验证，禁止由其产生当前合格。
            _legacy_nonpass(verdict, product.product_id, get("result_id"), allow_initial_pass=True)
    if review["decision"] != passes[-1]["decision"]:
        raise ValueError("存档总结果与末次审核不一致")
    if review["decision"] == "pass" and (len(passes) != 2 or any(p["decision"] != "pass" for p in passes)):
        raise ValueError("当前合格图缺少双次通过")
    return review["decision"]
