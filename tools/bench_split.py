"""Generate a full-size AOSP-shaped ninja graph for partition experiments.

Module counts per section come from a real `fission plan` of LineageOS 22.1,
so the shape is not invented.  Edge weights are synthetic -- a real run takes
them from out/.ninja_log instead (--ninja-log).

    python3 tools/bench_split.py --plan plan.json --modules 900 --workers 4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

RULES = """\
rule cc
  command = true
rule ar
  command = true
rule link
  command = true
rule install
  command = true
rule img
  command = true
"""


def build_graph(root: str, modules: int, projects: list[str], seed: int = 3) -> dict:
    rng = random.Random(seed)
    os.makedirs(root, exist_ok=True)
    lines = [RULES]
    installs: list[str] = []
    libs: list[str] = []
    log = ["# ninja log v5\n"]

    # Modules must live under real project paths, because that is how the
    # splitter attributes an edge to a project.
    by_head: dict[str, list[str]] = {}
    for p in projects:
        by_head.setdefault(p.split("/")[0], []).append(p)
    heads = [h for h in by_head if h in (
        "external", "packages", "system", "frameworks", "hardware",
        "device", "prebuilts", "art", "bionic", "libcore",
    )]
    for m in range(modules):
        head = rng.choice(heads)
        name = f"{rng.choice(by_head[head])}/mod{m:05d}"
        nsources = max(1, int(rng.lognormvariate(1.6, 0.8)))
        objs = []
        for i in range(nsources):
            src = f"{name}/src{i}.cc"
            obj = f"out/obj/{name}/src{i}.o"
            lines.append(f"build {obj}: cc {src}\n")
            secs = round(rng.lognormvariate(0.9, 0.9), 3)
            log.append(f"0\t{int(secs * 1000)}\t0\t{obj}\t0\n")
            objs.append(obj)
        lib = f"out/obj/{name}/lib.a"
        deps = ""
        if libs and rng.random() < 0.35:
            deps = " " + " ".join(rng.sample(libs, min(len(libs), rng.randint(1, 3))))
        lines.append(f"build {lib}: ar {' '.join(objs)}{deps}\n")
        secs = round(rng.lognormvariate(0.4, 0.7), 3)
        log.append(f"0\t{int(secs * 1000)}\t0\t{lib}\t0\n")
        libs.append(lib)
        inst = f"out/{name}/inst"
        lines.append(f"build {inst}: install {lib}\n")
        log.append(f"0\t20\t0\t{inst}\t0\n")
        installs.append(inst)

    lines.append(f"build out/product/system.img: img {' '.join(installs)}\n")
    log.append(f"0\t{int(rng.uniform(60, 300) * 1000)}\t0\tout/product/system.img\t0\n")

    with open(os.path.join(root, "combined.ninja"), "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    with open(os.path.join(root, ".ninja_log"), "w", encoding="utf-8") as fh:
        fh.write("".join(log))
    return {"modules": modules, "edges": sum(1 for line in lines if line.startswith("build "))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="plan.json from `fission plan` (gives the real section mix)")
    ap.add_argument("--modules", type=int, default=900)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--keep", help="keep the generated tree here")
    args = ap.parse_args()

    root = args.keep or tempfile.mkdtemp(prefix="fission-bench-")
    projects = [p["path"] for p in json.load(open(args.plan))["projects"]] if args.plan else []
    if not projects:
        projects = [f"{s}/mod" for s in (
            "external", "packages", "system", "frameworks",
            "hardware", "device", "prebuilts", "art", "bionic", "libcore",
        )]
    info = build_graph(root, args.modules, projects, args.seed)
    print(f"generated {info['edges']} edges under {root}")

    plan = {"digest": "bench", "policy": {"rules": []}, "projects": [{"path": p} for p in projects]}
    if args.plan:
        with open(args.plan, encoding="utf-8") as fh:
            real = json.load(fh)
        plan["policy"] = real["policy"]
        plan["projects"] = real["projects"]
        plan["digest"] = real["digest"]
    else:
        from fission import sections

        plan["policy"] = sections.Policy.default().as_dict()
    plan_path = os.path.join(root, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh)

    from fission import ninja, partition, sections

    t0 = time.time()
    graph = ninja.load([os.path.join(root, "combined.ninja")], root=root)
    t_parse = time.time() - t0
    weights = ninja.load_ninja_log(os.path.join(root, ".ninja_log"))
    policy = sections.Policy(plan["policy"]["rules"])
    projects = [p["path"] for p in plan["projects"]]

    print(f"parsed {len(graph.edges)} edges in {t_parse:.2f}s")
    for k in [1, 2, 4, 8, 16]:
        t0 = time.time()
        sp = partition.Splitter(graph, projects, policy, k, weights)
        res = sp.run()
        dt = time.time() - t0
        t = res.totals
        print(
            f"  workers={k:<3} split {dt:5.2f}s  serial {t['weight_serial']:8.0f}s  "
            f"crit {t['weight_critical_path']:7.0f}s  max-worker {t['max_worker_weight']:7.0f}s  "
            f"balance {t['balance']:.3f}  dup {t['duplication_factor']:.2f}x  "
            f"ideal {t['ideal_speedup']:.2f}x"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
