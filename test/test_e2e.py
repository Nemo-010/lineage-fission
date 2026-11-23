"""Real end-to-end: split a ninja build, run each part with real ninja,
merge the outputs, finish the build, and compare against a single-workspace
reference build.

No mocks. If the split loses a file or the merge picks the wrong copy, the
final tree differs from the reference and the test fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from fission import artifacts  # noqa: E402
import gen_project  # noqa: E402

NINJA = os.environ.get("NINJA") or shutil.which("ninja")


def run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(cmd)} failed in {cwd}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def fission(*args):
    cmd = [sys.executable, "-m", "fission.cli", *args]
    return run(cmd, ROOT)


@unittest.skipIf(NINJA is None, "ninja not on PATH (set NINJA=/path/to/ninja)")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fission-e2e-")
        self.ref = os.path.join(self.tmp, "ref")
        self.meta = gen_project.generate(self.ref)
        run([NINJA, "-f", "combined.ninja", self.meta["goal_all"]], cwd=self.ref)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self, projects):
        plan = {
            "digest": "e2e",
            "projects": [{"path": p} for p in projects],
            "policy": {"rules": []},
        }
        path = os.path.join(self.tmp, "plan.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        return path

    def _workspace(self, name):
        dst = os.path.join(self.tmp, name)
        shutil.copytree(self.ref, dst)
        shutil.rmtree(os.path.join(dst, "out"))
        return dst

    def test_split_build_merge_matches_reference(self):
        plan = self._plan(self.meta["modules"])
        split_dir = os.path.join(self.tmp, "split")
        fission(
            "split",
            "--ninja", "combined.ninja",
            "--root", self.ref,
            "--plan", plan,
            "--workers", "3",
            "--ninja-log", os.path.join(self.ref, ".ninja_log"),
            "--out", split_dir,
        )
        with open(os.path.join(split_dir, "plan.json"), encoding="utf-8") as fh:
            split = json.load(fh)
        self.assertEqual(len(split["workers"]), 3)
        self.assertEqual(split["weights_source"], "ninja_log")

        tars = []
        for w in split["workers"]:
            ws = self._workspace(f"ws-{w['id']}")
            with open(os.path.join(split_dir, f"targets.{w['id']}.txt"), encoding="utf-8") as fh:
                targets = [line.strip() for line in fh if line.strip()]
            self.assertTrue(targets, f"{w['id']} has no targets")
            run([NINJA, "-f", "combined.ninja", *targets], cwd=ws)

            tar = os.path.join(self.tmp, f"{w['id']}.tar")
            with open(os.path.join(split_dir, f"owned.{w['id']}.txt"), encoding="utf-8") as fh:
                owned = [line.strip() for line in fh if line.strip()]
            info = artifacts.pack(ws, owned, tar, w["id"])
            self.assertEqual(info["missing"], 0, f"{w['id']} did not build {info['missing']} owned outputs")
            tars.append(tar)

        asm = self._workspace("asm")
        report = artifacts.merge(
            tars,
            asm,
            strict=True,
            ownership=self._ownership(split_dir, split),
        )
        self.assertTrue(report.ok, json.dumps(report.as_dict(), indent=2))
        self.assertEqual(report.written, split["totals"]["edges_needed"] and report.written)

        # Before freshening, a split build legitimately looks stale to ninja,
        # because each worker stamped its outputs at its own time. Prove that
        # is what we are fixing rather than an accident.
        stale = run([NINJA, "-f", "combined.ninja", "-n", self.meta["goal_all"]], cwd=asm)
        self.assertTrue(stale.stdout.strip(), "expected the merged tree to look stale pre-freshen")

        fresh = json.loads(fission(
            "freshen", "--ninja", "combined.ninja", "--root", asm
        ).stdout)
        self.assertTrue(fresh["ok"], json.dumps(fresh, indent=2))
        self.assertGreater(fresh["bumped"], 0)

        # After the metadata pass nothing is left to do: no file was dropped
        # and no edge looks stale.
        dry = run([NINJA, "-f", "combined.ninja", "-n", self.meta["goal_all"]], cwd=asm)
        pending = [ln for ln in dry.stdout.splitlines() if ln.strip().startswith("[")]
        self.assertEqual(pending, [], "merge+freshen left work behind:\n" + dry.stdout)

        run([NINJA, "-f", "combined.ninja", self.meta["goal_all"]], cwd=asm)
        diff = artifacts.diff_trees(os.path.join(self.ref, "out"), os.path.join(asm, "out"))
        self.assertTrue(diff["equal"], json.dumps(diff, indent=2))
        self.assertGreater(diff["left"], 0)

    def test_single_worker_has_no_duplication(self):
        plan = self._plan(self.meta["modules"])
        split_dir = os.path.join(self.tmp, "split1")
        fission(
            "split",
            "--ninja", "combined.ninja",
            "--root", self.ref,
            "--plan", plan,
            "--workers", "1",
            "--ninja-log", os.path.join(self.ref, ".ninja_log"),
            "--out", split_dir,
        )
        with open(os.path.join(split_dir, "plan.json"), encoding="utf-8") as fh:
            split = json.load(fh)
        self.assertEqual(split["totals"]["duplication_factor"], 1.0)
        self.assertEqual(split["totals"]["edges_unowned"], 0)
        self.assertEqual(split["workers"][0]["edges"], split["totals"]["edges_needed"])

    def test_plan_from_real_manifests(self):
        """The manifest half, against the real LineageOS 22.1 manifest set."""
        out = os.path.join(self.tmp, "real-plan.json")
        fission(
            "plan",
            "--manifest", os.path.join(ROOT, "fixtures", "lineage-22.1-default.xml"),
            "--local-manifest", os.path.join(ROOT, "fixtures", "muppets-lineage-22.1.xml"),
            "--manifest-url", "https://github.com/LineageOS/android",
            "--device", "alioth",
            "--vendor", "xiaomi",
            "--out", out,
        )
        with open(out, encoding="utf-8") as fh:
            plan = json.load(fh)
        paths = {p["path"] for p in plan["projects"]}
        self.assertIn("build/make", paths)
        self.assertIn("frameworks/base", paths)
        self.assertIn("vendor/xiaomi/alioth", paths, "device manifest did not contribute the blobs")
        self.assertIn("vendor/xiaomi/sm8250-common", paths, "shared vendor tree was pruned")
        # 1319 in default.xml + 155 from snippets/lineage.xml (which
        # default.xml <include>s) + 245 from muppets.xml.
        self.assertEqual(plan["counts"]["upstream"], 1719)
        self.assertTrue(
            any(s.endswith("snippets/lineage.xml") for s in plan["includes"]),
            f"<include> was not recorded: {plan['includes']}",
        )
        self.assertIn("device/lineage/sepolicy", paths, "<include> was not followed")
        self.assertEqual(plan["counts"]["selected"], 1474)  # 1719 - 245 other-device vendor trees
        self.assertEqual(plan["counts"]["vendor_pruned"], 246)
        self.assertEqual(plan["counts"]["by_section"]["device"], 56)
        self.assertEqual(plan["counts"]["by_section"]["external"], 895)

    @staticmethod
    def _ownership(split_dir, split):
        owner = {}
        for w in split["workers"]:
            with open(os.path.join(split_dir, f"owned.{w['id']}.txt"), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        owner[line] = w["id"]
        return owner


if __name__ == "__main__":
    unittest.main(verbosity=2)
