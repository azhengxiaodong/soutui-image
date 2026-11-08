"""U_RULES：加载只含数据的审图规则；校验兼容范围并安全填充模板，不执行下载代码。"""

from __future__ import annotations

import hashlib
import json
import re
import string
from dataclasses import dataclass
from pathlib import Path

ENGINE = 1
FEATURES = ("颜色", "花纹与面料", "服装组成与版型", "领口与门襟", "袖子与袖口",
            "上衣下摆", "裤子与裤脚", "口袋", "扣子与配件", "其他变化",
            "袖口边缘结构", "门襟层次与边线", "扣位边缘与凸片", "裤长与脚踝位置")
FIELDS = {"body": {"product_id", "result_id", "review_kind", "attachments", "features_json"},
          "locator": set(), "buttons": {"attachments"}}


def canonical(value):
    """规则签名和哈希统一使用UTF-8规范JSON，避免换行差异造成误识别。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class RuleBundle:
    """一份不可变的规则快照；输入数据字节，输出模板和内容身份。"""
    raw: bytes

    @classmethod
    def parse(cls, raw):
        if len(raw) > 512 * 1024: raise ValueError("规则包超过大小上限")
        data = json.loads(raw)
        expected = {"format", "engine", "version", "sequence", "model", "timeout", "flow", "features", "prompts"}
        if set(data) != expected or data["format"] != 1 or data["engine"] != ENGINE:
            raise ValueError("规则格式或引擎版本不兼容，需要升级程序")
        if not isinstance(data["version"], str) or not re.fullmatch(r"[0-9][A-Za-z0-9._-]{0,63}", data["version"]):
            raise ValueError("规则版本无效")
        if type(data["sequence"]) is not int or data["sequence"] < 1: raise ValueError("规则序号无效")
        if data["model"] != "gpt-6.1-sol" or data["timeout"] != 600 or data["flow"] != "double_review_button":
            raise ValueError("本引擎仅支持已验证的Sol、600秒及双审加扣位专项，其他配置需另行验收")
        if tuple(data["features"]) != FEATURES: raise ValueError("检查项变化需升级引擎并重新验证")
        if not isinstance(data["prompts"], dict) or set(data["prompts"]) != set(FIELDS):
            raise ValueError("规则缺少整图、定位或扣位模板")
        for name, allowed in FIELDS.items():
            template = data["prompts"][name]
            if not isinstance(template, str) or not template.strip(): raise ValueError("提示词为空")
            actual = set()
            for _, field, spec, conversion in string.Formatter().parse(template):
                if field is not None:
                    if field not in allowed or spec or conversion: raise ValueError("模板包含非法字段或表达式")
                    actual.add(field)
            if actual != allowed: raise ValueError("模板缺少绑定图片/商品所需字段")
        return cls(canonical(data))

    @property
    def data(self): return json.loads(self.raw)
    @property
    def version(self): return self.data["version"]
    @property
    def sha256(self): return hashlib.sha256(self.raw).hexdigest()
    @property
    def sequence(self): return self.data["sequence"]
    @property
    def model(self): return self.data["model"]

    def render(self, name, **values):
        """只替换白名单字符串字段，绝不eval模板或执行Python。"""
        if name not in FIELDS or set(values) != FIELDS[name]: raise ValueError("提示词输入字段不符")
        if any(not isinstance(v, str) for v in values.values()): raise ValueError("提示词字段必须是文本")
        return self.data["prompts"][name].format_map(values)


def builtin_bundle():
    """程序自带已验收的初始规则，首次运行无需GitHub即可读取。"""
    return RuleBundle.parse((Path(__file__).parent / "resources" / "review_rules.json").read_bytes())


def pinned_builtin_bundles():
    """只保留真实未结束任务仍绑定的内置规则；固定内容哈希，不能借兼容入口改写旧标准。"""
    expected = {"2026-11-03.3": "05291e8a435275d69c9e789a3b273a4a94f45dba3561266150e7a3d467c87f84"}
    bundles = []
    for version, sha256 in expected.items():
        path = Path(__file__).parent / "resources" / f"review_rules_{version}.json"
        bundle = RuleBundle.parse(path.read_bytes())
        if bundle.version != version or bundle.sha256 != sha256:
            raise ValueError("任务兼容规则内容已改变，不能继续旧批次")
        bundles.append(bundle)
    return tuple(bundles)
