"""板块 C 数量、去重、商品绑定和重跑不换组的业务边界检查。"""

import copy
import hashlib
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from soutui_image.stage_a_inputs import ProductInput
from soutui_image.stage_c_web_plan import parse_favorites, prepare_references


def sample_favorites():
    folders = [{"id": i, "name": f"模特组{i}", "count": 5} for i in range(1, 18)]
    images = [{"id": i * 10 + j, "folderId": i, "mediaType": "image",
               "url": f"https://example.test/images/{i}_{j}.jpg"}
              for i in range(1, 18) for j in range(5)]
    images += [{"id": 1000 + j, "folderId": None, "mediaType": "image", "url": "unused"}
               for j in range(19)]
    return {"success": True, "folders": folders, "favorites": images, "total": len(images)}


class ReferencePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        original = self.root / "00123.jpg"
        original.write_bytes(b"original-content")
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        self.product = ProductInput("00123", str(original), 1, 1, "JPEG", digest)
        self.data = sample_favorites()

    def test_excludes_ungrouped_and_pins_45_references(self):
        groups = parse_favorites(self.data)
        self.assertEqual(len(groups), 17)
        self.assertEqual(sum(len(g.images) for g in groups), 85)
        plan = prepare_references(self.product, self.data, self.root / "state", rng=random.Random(7))
        self.assertEqual(plan.product_id, "00123")
        self.assertEqual([a.row for a in plan.assignments], list(range(1, 10)))
        self.assertEqual(len({a.folder_id for a in plan.assignments}), 9)
        self.assertEqual(sum(len(a.references) for a in plan.assignments), 45)

    def test_rerun_keeps_original_groups_and_file(self):
        state = self.root / "state"
        first = prepare_references(self.product, self.data, state, rng=random.Random(1))
        path = state / "product_00123_references.json"
        previous = path.read_bytes()
        second = prepare_references(self.product, self.data, state, rng=random.Random(999))
        self.assertEqual(first, second)
        self.assertEqual(path.read_bytes(), previous)

    def test_incomplete_response_or_group_count_stops(self):
        bad = []
        entry = copy.deepcopy(self.data); entry["total"] += 1; bad.append(entry)
        entry = copy.deepcopy(self.data); entry["folders"].pop(); bad.append(entry)
        entry = copy.deepcopy(self.data); entry["folders"][0]["count"] = 4; bad.append(entry)
        entry = copy.deepcopy(self.data); entry["favorites"].pop(0); entry["total"] -= 1; bad.append(entry)
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_favorites(data)

    def test_duplicate_group_or_image_stops(self):
        bad = []
        entry = copy.deepcopy(self.data); entry["folders"][1]["id"] = 1; bad.append(entry)
        entry = copy.deepcopy(self.data); entry["folders"][1]["name"] = "模特组1"; bad.append(entry)
        entry = copy.deepcopy(self.data); entry["favorites"][1]["id"] = entry["favorites"][0]["id"]; bad.append(entry)
        entry = copy.deepcopy(self.data); entry["favorites"][1]["url"] = entry["favorites"][0]["url"]; bad.append(entry)
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_favorites(data)

    def test_changed_reference_does_not_replace_saved_plan(self):
        state = self.root / "state"
        plan = prepare_references(self.product, self.data, state, rng=random.Random(3))
        path = state / "product_00123_references.json"
        previous = path.read_bytes()
        chosen_id = int(plan.assignments[0].references[0].image_id)
        data = copy.deepcopy(self.data)
        next(x for x in data["favorites"] if x["id"] == chosen_id)["url"] = "https://example.test/changed.jpg"
        with self.assertRaises(ValueError):
            prepare_references(self.product, data, state)
        self.assertEqual(path.read_bytes(), previous)

    def test_wrong_source_binding_or_changed_source_stops(self):
        state = self.root / "state"
        prepare_references(self.product, self.data, state)
        with self.assertRaises(ValueError):
            prepare_references(replace(self.product, sha256="wrong"), self.data, state)
        Path(self.product.source_path).write_bytes(b"changed-content")
        with self.assertRaises(ValueError):
            prepare_references(self.product, self.data, state)


if __name__ == "__main__":
    unittest.main()
