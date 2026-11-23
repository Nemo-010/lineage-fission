"""Unit tests for the pack/merge contract, including the traps we hit."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from fission import artifacts  # noqa: E402


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fission-art-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tree(self, name, files):
        root = os.path.join(self.tmp, name)
        for rel, content in files.items():
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
        return root

    def test_mtime_survives_to_the_nanosecond(self):
        """tar keeps whole seconds; the manifest is what makes merge exact.

        Getting this wrong made every merged output look older than its inputs
        and ninja rebuilt the entire tree on the assembler.
        """
        root = self._tree("a", {"out/x": "x"})
        path = os.path.join(root, "out/x")
        want = 1_795_363_447_024_032_523
        os.utime(path, ns=(want, want))

        tar = os.path.join(self.tmp, "a.tar")
        artifacts.pack(root, ["out/x"], tar, "w0")
        dst = os.path.join(self.tmp, "dst")
        os.makedirs(dst)
        artifacts.merge([tar], dst)

        self.assertEqual(os.stat(os.path.join(dst, "out/x")).st_mtime_ns, want)

    def test_identical_files_are_deduped(self):
        a = self._tree("a", {"out/shared": "same"})
        b = self._tree("b", {"out/shared": "same", "out/b_only": "b"})
        ta, tb = os.path.join(self.tmp, "a.tar"), os.path.join(self.tmp, "b.tar")
        artifacts.pack(a, ["out/shared"], ta, "w0")
        artifacts.pack(b, ["out/shared", "out/b_only"], tb, "w1")

        dst = os.path.join(self.tmp, "dst")
        os.makedirs(dst)
        report = artifacts.merge([ta, tb], dst)
        self.assertEqual(report.deduped, 1)
        self.assertEqual(report.collided, [])
        self.assertEqual(report.written, 2)
        self.assertTrue(report.ok)

    def test_collision_goes_to_the_assigned_owner(self):
        a = self._tree("a", {"out/x": "from-a"})
        b = self._tree("b", {"out/x": "from-b"})
        ta, tb = os.path.join(self.tmp, "a.tar"), os.path.join(self.tmp, "b.tar")
        artifacts.pack(a, ["out/x"], ta, "w0")
        artifacts.pack(b, ["out/x"], tb, "w1")

        dst = os.path.join(self.tmp, "dst")
        os.makedirs(dst)
        report = artifacts.merge([ta, tb], dst, ownership={"out/x": "w1"})
        self.assertEqual(len(report.collided), 1)
        with open(os.path.join(dst, "out/x"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "from-b")

        with self.assertRaises(artifacts.MergeError):
            dst2 = os.path.join(self.tmp, "dst2")
            os.makedirs(dst2)
            artifacts.merge([ta, tb], dst2, fail_on_collision=True, ownership={"out/x": "w1"})

    def test_missing_file_in_artifact_is_reported(self):
        root = self._tree("a", {"out/x": "x"})
        tar = os.path.join(self.tmp, "a.tar")
        info = artifacts.pack(root, ["out/x", "out/never_built"], tar, "w0")
        self.assertEqual(info["missing"], 1)

    def test_tar_without_manifest_is_rejected(self):
        import tarfile

        root = self._tree("a", {"out/x": "x"})
        tar = os.path.join(self.tmp, "raw.tar")
        with tarfile.open(tar, "w") as tf:
            tf.add(os.path.join(root, "out/x"), arcname="out/x")
        dst = os.path.join(self.tmp, "dst")
        os.makedirs(dst)
        with self.assertRaises(artifacts.MergeError):
            artifacts.merge([tar], dst)

    def test_diff_trees_compares_content_not_timestamps(self):
        a = self._tree("a", {"f": "1", "g": "2"})
        b = self._tree("b", {"f": "1", "g": "2"})
        os.utime(os.path.join(b, "f"), ns=(1, 1))
        result = artifacts.diff_trees(a, b)
        self.assertTrue(result["equal"], json.dumps(result))

        with open(os.path.join(b, "g"), "w", encoding="utf-8") as fh:
            fh.write("3")
        result = artifacts.diff_trees(a, b)
        self.assertFalse(result["equal"])
        self.assertEqual(result["differing"], ["g"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
