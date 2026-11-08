"""内置标准升级时，真实旧任务的规则身份、人工结论和缓存不得被静默重置。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from soutui_image.rule_store import RuleStore, policy_id, write_json
from soutui_image.rules_bundle import RuleBundle, builtin_bundle, pinned_builtin_bundles
from soutui_image.rule_runtime import current_policy, use_rules


class PinnedBuiltinRulesTests(unittest.TestCase):
    def test_old_task_keeps_identity_and_new_task_uses_current_standard(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RuleStore(directory,'unused-offline-key')
            old=pinned_builtin_bundles()[0]; new=builtin_bundle()
            tasks=Path(directory)/'tasks'
            product=SimpleNamespace(product_id='100',sha256='a'*64)
            pin=tasks/'product_100_rules.json'
            write_json(pin,{'product_id':'100','source_sha256':product.sha256,'bundle_sha256':old.sha256,'policy_id':old.version})
            before=pin.read_bytes()
            self.assertEqual(store.for_product(product,tasks).raw,old.raw)
            self.assertEqual(pin.read_bytes(),before)
            with use_rules(old): self.assertEqual(current_policy(),'2026-11-03.3')
            fresh=SimpleNamespace(product_id='101',sha256='b'*64)
            self.assertEqual(store.for_product(fresh,tasks).raw,new.raw)
            self.assertEqual(store.latest().raw,new.raw)

    def test_changed_content_cannot_impersonate_pinned_old_version(self):
        old=pinned_builtin_bundles()[0]
        data=old.data; data['prompts']['body']+='\n篡改旧标准。'
        changed=RuleBundle.parse(json.dumps(data,ensure_ascii=False).encode('utf-8'))
        with self.assertRaises(ValueError):policy_id(changed)

    def test_new_policy_cannot_share_old_cache_identity(self):
        self.assertNotEqual(policy_id(builtin_bundle()),policy_id(pinned_builtin_bundles()[0]))
        self.assertNotEqual(builtin_bundle().sha256,pinned_builtin_bundles()[0].sha256)
