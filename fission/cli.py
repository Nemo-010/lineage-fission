"""Command line: plan -> split -> (workers) -> merge -> verify."""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

from . import artifacts, freshen as fr, manifest as mf, ninja, partition, sections


def _fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"fission: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _local_manifests(tree: str) -> list[str]:
    return sorted(glob.glob(os.path.join(tree, ".repo", "local_manifests", "*.xml")))


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace) -> int:
    files: list[str] = []
    files.extend(args.manifest)
    if args.tree:
        files.extend(_local_manifests(args.tree))
    files.extend(args.local_manifest)

    manifest_url = args.manifest_url
    if not manifest_url and args.tree:
        manifest_url = "https://github.com/LineageOS/android"

    m = mf.parse(
        files,
        manifest_url=manifest_url,
        include_dirs=args.include_dir,
        fetch_includes=args.fetch_includes,
        cache_dir=args.include_cache,
    )
    upstream = len(m.projects)
    if args.group or args.exclude_group:
        m = m.select(groups=args.group, exclude=args.exclude_group)

    vendor_dropped = 0
    if args.device:
        vendor_dropped, _dropped = m.prune_device(args.device)
        if args.vendor:
            device_path = f"device/{args.vendor}/{args.device}"
            if device_path not in m.projects:
                m.add_project(
                    f"LineageOS/android_device_{args.vendor}_{args.device}",
                    device_path,
                    origin="roomservice",
                )
        elif not any(p.startswith(f"device/") and p.endswith(f"/{args.device}") for p in m.projects):
            print(
                f"fission: warning: no device tree for {args.device} in the manifest set; "
                f"pass --vendor <oem> to add device/<oem>/{args.device}",
                file=sys.stderr,
            )
    selected = len(m.projects)

    if args.expand_deps:
        if args.expand_deps == "tree" and not args.tree:
            _fail("--expand-deps tree needs --tree DIR")
        reader = mf.TreeReader(args.tree) if args.expand_deps == "tree" else mf.HttpReader()
        before = len(m.projects)
        m = mf.expand_dependencies(m, reader)
        print(
            f"fission: lineage.dependencies added {len(m.projects) - before} project(s)",
            file=sys.stderr,
        )

    policy = sections.Policy.load(args.sections) if args.sections else sections.Policy.default()
    grouped = sections.sections_for_projects(sorted(m.projects), policy)

    plan = {
        "digest": m.digest(),
        "manifest_url": m.manifest_url,
        "sources": m.sources,
        "includes": m.includes,
        "groups": sorted(args.group),
        "counts": {
            "upstream": upstream,
            "selected": selected,
            "vendor_pruned": vendor_dropped,
            "after_dependencies": len(m.projects),
            "by_section": {k: len(v) for k, v in sorted(grouped.items())},
        },
        "sections": grouped,
        "projects": [m.projects[p].as_dict() for p in sorted(m.projects)],
        "policy": policy.as_dict(),
    }
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"fission: wrote {args.out}", file=sys.stderr)
    print(
        f"fission: {upstream} upstream -> {selected} selected -> "
        f"{len(m.projects)} with dependencies, digest {m.digest()[:12]}"
    )
    for sec, projects in sorted(grouped.items()):
        print(f"  {sec:<12} {len(projects):>4} projects")
    return 0


# --------------------------------------------------------------------------
# split
# --------------------------------------------------------------------------

def cmd_split(args: argparse.Namespace) -> int:
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    policy = (
        sections.Policy.from_dict(plan["policy"])
        if plan.get("policy")
        else sections.Policy.default()
    )
    projects = [p["path"] for p in plan["projects"]]

    weights = ninja.load_ninja_log(args.ninja_log) if args.ninja_log else {}
    graph = ninja.load(args.ninja, root=args.root)

    splitter = partition.Splitter(graph, projects, policy, args.workers, weights)
    result = splitter.run()
    result.digest = plan.get("digest", "")
    if args.ninja_log and not weights:
        print("fission: warning: ninja log was empty, fell back to rule weights", file=sys.stderr)

    if args.out:
        result.write(args.out)
        # Ownership lists: every output the worker is assigned, so the merge
        # knows who decides when two workers disagree about the bytes.
        by_worker: dict[str, list[str]] = {w.id: [] for w in result.workers}
        for e in graph.edges:
            wid = splitter.edge_worker[e.index]
            if wid is None:
                continue
            by_worker[wid].extend(e.outputs)
        for wid, outs in by_worker.items():
            with open(os.path.join(args.out, f"owned.{wid}.txt"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(sorted(set(outs))))
                fh.write("\n")
        print(f"fission: wrote {args.out}/plan.json", file=sys.stderr)

    t = result.totals
    print(
        f"fission: {t['edges_total']} edges, {t['edges_needed']} needed, "
        f"{t['units']} units over {len(result.workers)} workers"
    )
    print(
        f"  serial {t['weight_serial']:.0f}s  critical-path {t['weight_critical_path']:.0f}s  "
        f"ideal speedup {t['ideal_speedup']:.2f}x  duplication {t['duplication_factor']:.2f}x"
    )
    print(f"  balance {t['balance']:.3f} (1.0 = perfect), unowned edges {t['edges_unowned']}")
    for w in result.workers:
        print(
            f"  {w.id}: {','.join(w.sections):<28} {w.edges:>6} edges {w.weight:>8.0f} "
            f"({w.closure_weight:>8.0f} incl. deps, {len(w.targets)} targets)"
        )
    if t["edges_unowned"]:
        print(
            "fission: warning: some needed edges have no owner; the assembler will build them",
            file=sys.stderr,
        )
    return 0


# --------------------------------------------------------------------------
# pack / merge / verify
# --------------------------------------------------------------------------

def cmd_pack(args: argparse.Namespace) -> int:
    with open(args.targets, encoding="utf-8") as fh:
        paths = [line.strip() for line in fh if line.strip()]
    info = artifacts.pack(args.root, paths, args.tar, args.worker)
    print(json.dumps(info, indent=2))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    ownership: dict[str, str] = {}
    if args.ownership_dir:
        for path in sorted(glob.glob(os.path.join(args.ownership_dir, "owned.*.txt"))):
            worker = os.path.basename(path)[len("owned."):-len(".txt")]
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        ownership[line] = worker
    report = artifacts.merge(
        sorted(args.artifact),
        args.dest,
        strict=args.strict,
        fail_on_collision=args.fail_on_collision,
        ownership=ownership,
    )
    print(json.dumps(report.as_dict(), indent=2))
    if not report.ok:
        _fail("merge verification failed")
    return 0


def cmd_freshen(args: argparse.Namespace) -> int:
    graph = ninja.load(args.ninja, root=args.root)
    log_path = args.ninja_log or os.path.join(args.root, ".ninja_log")
    report = fr.freshen(graph, args.root, dry_run=args.dry_run, log_path=log_path)
    print(json.dumps(report.as_dict(), indent=2))
    if not report.ok:
        _fail(f"{len(report.missing_inputs)} inputs are missing from the merged tree")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    result = artifacts.diff_trees(args.left, args.right)
    print(json.dumps(result, indent=2))
    if not result["equal"]:
        _fail("trees differ")
    return 0


# --------------------------------------------------------------------------
# measure / doctor -- the instrument
# --------------------------------------------------------------------------

def _tree_size(path: str) -> tuple[int, int]:
    total = 0
    count = 0
    for dirpath, dirnames, files in os.walk(path):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
                count += 1
            except OSError:
                pass
    return total, count


def cmd_measure(args: argparse.Namespace) -> int:
    out = {"tree": args.tree, "paths": {}}
    targets = args.path or ["."]
    for t in targets:
        full = os.path.join(args.tree, t)
        if not os.path.exists(full):
            out["paths"][t] = {"exists": False}
            continue
        size, count = _tree_size(full)
        out["paths"][t] = {"exists": True, "bytes": size, "files": count}
    usage = shutil.disk_usage(args.tree)
    out["disk"] = {"free": usage.free, "total": usage.total}
    print(json.dumps(out, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Say whether this machine can hold a LineageOS build, in measured numbers."""
    root = args.root
    usage = shutil.disk_usage(root)
    mem_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    cpu = os.cpu_count() or 1
    free_gb = usage.free / 1e9
    mem_gb = mem_bytes / 1e9
    ok = True
    checks = [
        ("disk free", free_gb, args.min_free_gb, "GB"),
        ("memory", mem_gb, args.min_ram_gb, "GB"),
        ("cpus", float(cpu), float(args.min_cpus), ""),
    ]
    for name, have, want, unit in checks:
        good = have >= want
        ok = ok and good
        print(f"{'ok  ' if good else 'FAIL'} {name:<10} {have:8.1f}{unit} (need >= {want}{unit})")
    print(f"     root       {root}")
    print(f"     ninja      {shutil.which('ninja') or 'MISSING'}")
    print(f"     ccache     {shutil.which('ccache') or '-'}  sccache {shutil.which('sccache') or '-'}")
    if not ok:
        print(
            "fission: this machine cannot hold a LineageOS build; "
            "a standard GitHub-hosted runner has 14 GB of disk and will fail here.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fission", description="split a LineageOS build across workers")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan", help="resolve manifests into a project/section plan")
    pp.add_argument("--manifest", action="append", default=[], help="upstream manifest xml")
    pp.add_argument("--local-manifest", action="append", default=[], help="extra manifest xml")
    pp.add_argument("--tree", help="synced source tree (reads .repo/local_manifests/*.xml)")
    pp.add_argument("--manifest-url", default="", help="url of the manifest repository")
    pp.add_argument("--include-dir", action="append", default=[], help="where <include> files live")
    pp.add_argument("--fetch-includes", action="store_true", help="fetch <include> files from the manifest repo")
    pp.add_argument("--include-cache", help="directory to keep fetched includes in")
    pp.add_argument("--device", help="device codename; prunes muppets trees to muppets_<device>")
    pp.add_argument("--vendor", help="oem for the device tree, e.g. xiaomi")
    pp.add_argument("--group", action="append", default=[], help="extra repo group to enable")
    pp.add_argument("--exclude-group", action="append", default=[])
    pp.add_argument("--expand-deps", choices=["http", "tree"], help="follow lineage.dependencies")
    pp.add_argument("--sections", help="section policy json")
    pp.add_argument("--out", help="write plan json here")
    pp.set_defaults(func=cmd_plan)

    ps = sub.add_parser("split", help="partition the ninja graph into worker target lists")
    ps.add_argument("--ninja", action="append", required=True)
    ps.add_argument("--root", help="source tree root (chdir before reading ninja files)")
    ps.add_argument("--plan", required=True)
    ps.add_argument("--workers", type=int, required=True)
    ps.add_argument("--ninja-log", help="out/.ninja_log from a previous full build")
    ps.add_argument("--out", help="write plan.json + targets.*.txt + owned.*.txt here")
    ps.set_defaults(func=cmd_split)

    pk = sub.add_parser("pack", help="worker: tar the files this worker built")
    pk.add_argument("--root", required=True)
    pk.add_argument("--targets", required=True, help="file with one path per line")
    pk.add_argument("--tar", required=True)
    pk.add_argument("--worker", required=True)
    pk.set_defaults(func=cmd_pack)

    pm = sub.add_parser("merge", help="assembler: merge worker artifacts")
    pm.add_argument("--artifact", action="append", required=True)
    pm.add_argument("--dest", required=True)
    pm.add_argument("--ownership-dir", help="directory holding owned.*.txt")
    pm.add_argument("--strict", action="store_true", help="fail on unowned or unverified paths")
    pm.add_argument("--fail-on-collision", action="store_true", help="fail if two workers disagree")
    pm.set_defaults(func=cmd_merge)

    pf = sub.add_parser("freshen", help="assembler: bump mtimes so ninja sees no stale edges")
    pf.add_argument("--ninja", action="append", required=True)
    pf.add_argument("--root", required=True)
    pf.add_argument("--ninja-log", help="build log to update (default <root>/.ninja_log)")
    pf.add_argument("--dry-run", action="store_true")
    pf.set_defaults(func=cmd_freshen)

    pmx = sub.add_parser("matrix", help="emit the workflow matrix + one local manifest per shard")
    pmx.add_argument("--plan", required=True, help="plan.json from `fission plan`")
    pmx.add_argument("--split-plan", required=True, help="plan.json from `fission split`")
    pmx.add_argument("--out", required=True)
    pmx.set_defaults(func=cmd_matrix)

    pv = sub.add_parser("verify", help="compare two trees by content")
    pv.add_argument("--left", required=True)
    pv.add_argument("--right", required=True)
    pv.set_defaults(func=cmd_verify)

    pme = sub.add_parser("measure", help="report real sizes of a tree")
    pme.add_argument("--tree", required=True)
    pme.add_argument("--path", action="append")
    pme.set_defaults(func=cmd_measure)

    pd = sub.add_parser("doctor", help="check this machine against build requirements")
    pd.add_argument("--root", default=".")
    pd.add_argument("--min-free-gb", type=float, default=150.0)
    pd.add_argument("--min-ram-gb", type=float, default=16.0)
    pd.add_argument("--min-cpus", type=int, default=4)
    pd.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    return args.func(args)



# --------------------------------------------------------------------------
# matrix -- one repository, one workflow, up to 256 shards
# --------------------------------------------------------------------------

MAX_MATRIX = 256


def cmd_matrix(args: argparse.Namespace) -> int:
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    with open(args.split_plan, encoding="utf-8") as fh:
        split = json.load(fh)

    workers = split["workers"]
    if len(workers) > MAX_MATRIX:
        _fail(
            f"{len(workers)} shards exceeds GitHub's limit of {MAX_MATRIX} jobs per "
            f"matrix; re-run split with --workers {MAX_MATRIX}"
        )

    projects = {p["path"]: p for p in plan["projects"]}
    by_project: dict[str, dict] = {}
    for p in plan["projects"]:
        by_project.setdefault(p["path"], p)

    out_manifests = os.path.join(args.out, "local_manifests")
    os.makedirs(out_manifests, exist_ok=True)

    include = []
    for w in workers:
        shard = w["id"]
        # One local manifest per shard: this is what makes a 14 GB runner able
        # to sync anything at all -- it asks repo for its own projects only.
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<manifest>"]
        for path in w["projects"]:
            proj = by_project.get(path)
            if proj is None:
                continue
            lines.append(
                f'  <project name="{proj["name"]}" path="{proj["path"]}" '
                f'remote="{proj["remote"]}" revision="{proj["revision"]}" />'
            )
        lines.append("</manifest>")
        with open(os.path.join(out_manifests, f"shard-{shard}.xml"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        include.append(
            {
                "shard": shard,
                "sections": ",".join(w["sections"]),
                "projects": len(w["projects"]),
                "edges": w["edges"],
                "weight": w["weight"],
                "targets": f"targets.{shard}.txt",
                "owned": f"owned.{shard}.txt",
                "manifest": f"local_manifests/shard-{shard}.xml",
            }
        )

    matrix = {"include": include}
    with open(os.path.join(args.out, "matrix.json"), "w", encoding="utf-8") as fh:
        json.dump(matrix, fh, indent=2, sort_keys=True)
        fh.write("\n")

    totals = dict(split["totals"])
    totals["shards"] = len(include)
    totals["matrix_limit"] = MAX_MATRIX
    totals["digest"] = plan.get("digest", "")
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(totals, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(json.dumps({"shards": len(include), "max": MAX_MATRIX, "totals": totals}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
