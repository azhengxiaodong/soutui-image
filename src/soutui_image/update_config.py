"""更新源与软件版本：只包含公开校验信息，不携带维护端私钥或访问令牌。"""

import json
from pathlib import Path

from .rule_store import RuleStore,read
from .rules_bundle import builtin_bundle


APP_VERSION="0.2.11"


def update_source():
    """从程序自带资源读取固定仓库和信任公钥，不接受远程清单替换公钥。"""
    return json.loads((Path(__file__).parent/"resources/update_source.json").read_text(encoding="utf-8"))


def make_rule_store(data_directory):
    return RuleStore(data_directory,update_source()["public_key"])


def display_bundle(data_directory,product):
    """界面只读显示商品固定版本，不因扫描目录就升级或创建任务。"""
    store=make_rule_store(data_directory)
    path=Path(data_directory)/"tasks"/f"product_{product.product_id}_rules.json"
    if path.exists():
        record=read(path)
        if record["source_sha256"]!=product.sha256: raise ValueError("商品规则与原图身份不符")
        return store.load(record["bundle_sha256"])
    historical=list((Path(data_directory)/"tasks"/product.product_id).glob("group_*/round_*.json"))
    return builtin_bundle() if historical else store.latest()
