"""Section policy: mapping, and the round trip through plan.json."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fission import sections  # noqa: E402


class PolicyTests(unittest.TestCase):
    def test_default_mapping(self):
        p = sections.Policy.default()
        self.assertEqual(p.section_of("external/zlib"), "external")
        self.assertEqual(p.section_of("frameworks/base"), "frameworks")
        self.assertEqual(p.section_of("device/xiaomi/alioth"), "device")
        self.assertEqual(p.section_of("vendor/xiaomi/alioth"), "device")
        self.assertEqual(p.section_of("prebuilts/clang/host/linux-x86"), "prebuilts")
        self.assertEqual(p.section_of("art"), "runtime")
        self.assertEqual(p.section_of("bionic"), "runtime")
        self.assertEqual(p.section_of("out/soong/build.ninja"), "out")
        self.assertEqual(p.section_of("weird/top-level"), "misc")

    def test_round_trip_through_json(self):
        """as_dict -> from_dict must preserve every rule, or split misfiles
        every project into 'misc' and the balance numbers become a lie."""
        original = sections.Policy.default()
        restored = sections.Policy.from_dict(json.loads(json.dumps(original.as_dict())))
        self.assertEqual(original.rules, restored.rules)
        for path in ("external/zlib", "frameworks/base", "art", "packages/Settings"):
            self.assertEqual(original.section_of(path), restored.section_of(path))

    def test_bad_rules_are_rejected(self):
        with self.assertRaises(ValueError):
            sections.Policy.from_dict({"rules": [{"prefix": "external"}]})
        with self.assertRaises(ValueError):
            sections.Policy.from_dict({"rules": ["external"]})

    def test_sections_for_projects_groups_sorted(self):
        p = sections.Policy.default()
        got = sections.sections_for_projects(["external/b", "external/a", "art"], p)
        self.assertEqual(got["external"], ["external/a", "external/b"])
        self.assertEqual(got["runtime"], ["art"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
