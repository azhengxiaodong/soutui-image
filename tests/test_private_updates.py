"""私有更新边界：签名伪造、损坏包、旧清单与同版替换均不能激活。"""

import base64
import copy
import hashlib
import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from soutui_image.rules_bundle import builtin_bundle, RuleBundle, canonical
from soutui_image.rule_store import RuleStore
from soutui_image.update_security import verify_payload
from soutui_image.github_updates import GitHubReader
from soutui_image.update_service import UpdateService


class PrivateUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.key=Ed25519PrivateKey.generate()
        self.public=base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
        self.store=RuleStore(Path(self.tmp.name),self.public)

    def envelope(self,bundle,sequence=1):
        p={"format":1,"sequence":sequence,"repository_id":123,"channel":"stable","rules":{
            "id":1,"size":len(bundle.raw),"sha256":bundle.sha256,"version":bundle.version},"program":None,"revoked":[]}
        return {"payload":p,"signature":base64.b64encode(self.key.sign(canonical(p))).decode()}

    def test_forgery_or_corruption_leaves_current_unchanged(self):
        b=builtin_bundle(); e=self.envelope(b); self.store.install(e,b.raw,repository_id=123)
        before=(self.store.root/"stable.json").read_bytes()
        bad=copy.deepcopy(e); bad["payload"]["sequence"]=2
        with self.assertRaises(ValueError): self.store.install(bad,b.raw,repository_id=123)
        with self.assertRaises(ValueError): self.store.install(e,b.raw+b" ",repository_id=123)
        self.assertEqual(before,(self.store.root/"stable.json").read_bytes())

    def test_task_pin_and_new_task_selection(self):
        first=builtin_bundle(); self.store.install(self.envelope(first),first.raw,repository_id=123)
        tasks=self.store.data/"tasks"
        p=SimpleNamespace(product_id="100",sha256="a"*64)
        self.assertEqual(self.store.for_product(p,tasks).sha256,first.sha256)
        d=first.data; d.update(version="2099-01-01.1",sequence=first.sequence+1)
        second=RuleBundle.parse(json.dumps(d).encode()); self.store.install(self.envelope(second,2),second.raw,repository_id=123)
        self.assertEqual(self.store.for_product(p,tasks).sha256,first.sha256)
        self.assertEqual(self.store.for_product(SimpleNamespace(product_id="101",sha256="b"*64),tasks).sha256,second.sha256)
        with self.assertRaises(ValueError): self.store.install(self.envelope(first),first.raw,repository_id=123)

    def test_reader_cannot_write_or_access_other_repository(self):
        reader=GitHubReader("owner","repo","test-token")
        with self.assertRaises(ValueError): reader._request("POST",reader.prefix+"/releases",value={})
        with self.assertRaises(ValueError): reader._request("GET","/repos/owner/repo-extra")

    def test_wrong_public_key_rejected(self):
        public=base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
        with self.assertRaises(ValueError): verify_payload(self.envelope(builtin_bundle()),public)

    def test_remote_older_rules_cannot_replace_new_builtin_standard(self):
        first=builtin_bundle(); self.store.install(self.envelope(first),first.raw,repository_id=123)
        before=(self.store.root/"stable.json").read_bytes()
        data=first.data; data.update(version="2020-01-01.1",sequence=max(1,first.sequence-1))
        older=RuleBundle.parse(json.dumps(data).encode())
        if older.sequence < first.sequence:
            with self.assertRaises(ValueError): self.store.install(self.envelope(older,2),older.raw,repository_id=123)
            self.assertEqual(before,(self.store.root/"stable.json").read_bytes())

    def test_incompatible_rules_preserve_current_but_expose_program_upgrade(self):
        first=builtin_bundle(); self.store.install(self.envelope(first),first.raw,repository_id=123)
        raw=canonical({**first.data,"engine":2,"version":"2026-10-30.1"})
        payload=self.envelope(first,2)["payload"]
        payload["rules"]={"id":2,"size":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"version":"2026-10-30.1"}
        payload["program"]={"id":3,"size":100,"sha256":"a"*64,"version":"0.3.0"}
        envelope={"payload":payload,"signature":base64.b64encode(self.key.sign(canonical(payload))).decode()}
        class Reader:
            def repository_info(s): return {"id":123}
            def release(s,tag): return {"assets":[{"name":"update.json","id":1,"size":len(canonical(envelope))}],"html_url":"private-test"}
            def download_asset(s,a,**kw): return canonical(envelope) if a["id"]==1 else raw
        with self.assertRaises(ValueError): self.store.sync(Reader())
        self.assertEqual(self.store.latest().sha256,first.sha256)
        service=object.__new__(UpdateService); service.store=self.store
        self.assertEqual(service.program_offer()["version"],"0.3.0")
        payload["sequence"]=3; payload["revoked"]=[first.version]
        revoked={"payload":payload,"signature":base64.b64encode(self.key.sign(canonical(payload))).decode()}
        class RevokedReader(Reader):
            def download_asset(s,a,**kw): return canonical(revoked) if a["id"]==1 else raw
        with self.assertRaises(ValueError): self.store.sync(RevokedReader())
        with self.assertRaises(ValueError): self.store.latest()
        with self.assertRaises(ValueError): self.store.for_product(SimpleNamespace(product_id="200",sha256="c"*64),self.store.data/"tasks")
