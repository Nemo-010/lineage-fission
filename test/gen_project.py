"""Generate a small but Android-shaped ninja project.

Same topology as a real AOSP build, four orders of magnitude smaller: source
files, one compile edge each, a per-module archive, an install edge, and a
final image edge that consumes every install.  Cross-section links are added
on purpose, because those are what make a split expensive.

Used by test_e2e.py, which builds it for real with ninja.
"""

from __future__ import annotations

import json
import os
import random

RULES = """\
rule cc
  command = cat $in > $out
  description = CC $out
rule ar
  command = cat $in > $out
  description = AR $out
rule install
  command = cp $in $out
  description = INSTALL $out
rule img
  command = cat $in > $out
  description = IMAGE $out
"""

# (section, module, sources, links_to)
LAYOUT = [
    ("system", "libcore", 4, []),
    ("system", "libbase", 3, ["system/libcore"]),
    ("frameworks", "base", 6, ["system/libbase"]),
    ("frameworks", "native", 4, ["frameworks/base"]),
    ("external", "libpng", 5, ["system/libbase"]),
    ("external", "libjpeg", 3, ["system/libbase"]),
    ("external", "zlib", 2, []),
    ("external", "selinux", 3, ["system/libcore"]),
    ("external", "libxml2", 4, ["external/zlib"]),
    ("hardware", "libhardware", 3, ["system/libbase"]),
    ("packages", "Settings", 2, ["frameworks/base"]),
    ("packages", "SystemUI", 3, ["frameworks/base", "frameworks/native"]),
    ("device", "acme/phone", 2, ["hardware/libhardware", "external/libpng"]),
    ("prebuilts", "tool", 1, []),
]


def generate(root: str, seed: int = 7) -> dict:
    rng = random.Random(seed)
    os.makedirs(root, exist_ok=True)
    lines = [RULES]
    installs: list[str] = []

    for section, module, nsources, links in LAYOUT:
        src_dir = os.path.join(root, section, module)
        os.makedirs(src_dir, exist_ok=True)
        objs = []
        for i in range(nsources):
            rel = f"{section}/{module}/s{i}.txt"
            with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
                fh.write(f"{section}/{module} source {i}\n" * (i + 1))
            obj = f"out/obj/{section}/{module}/s{i}.o"
            lines.append(f"build {obj}: cc {rel}\n")
            objs.append(obj)
        extra = []
        for link in links:
            extra.append(f"out/obj/{link}/lib.a")
        lib = f"out/obj/{section}/{module}/lib.a"
        ins = " ".join(objs + extra)
        lines.append(f"build {lib}: ar {ins}\n")
        inst = f"out/{section}/{module}/inst"
        lines.append(f"build {inst}: install {lib}\n")
        installs.append(inst)

    lines.append(f"build out/product/system.img: img {' '.join(installs)}\n")
    lines.append("build out/product/target_files.zip: img out/product/system.img\n")
    lines.append("default out/product/system.img\n")

    with open(os.path.join(root, "combined.ninja"), "w", encoding="utf-8") as fh:
        fh.write("".join(lines))

    meta = {
        "goal": "out/product/system.img",
        "goal_all": "out/product/target_files.zip",
        "modules": [f"{s}/{m}" for s, m, _, _ in LAYOUT],
        "installs": installs,
    }
    with open(os.path.join(root, "project.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return meta


if __name__ == "__main__":
    import sys

    print(json.dumps(generate(sys.argv[1] if len(sys.argv) > 1 else "e2e-project"), indent=2))
