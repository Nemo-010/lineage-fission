"""Make a merged tree consistent for ninja, without rebuilding anything.

The problem this solves is structural, not cosmetic.  Ninja decides staleness
from mtimes: an edge is dirty if any output is older than its newest input.
When a build is split across workers, each worker stamps its own outputs at its
own wall-clock time, so after a merge an output can be older than an input that
a *different* worker built later:

    recorded mtime of out/obj/system/libcore/lib.a older than most recent input
    out/obj/system/libcore/s1.o (1795363456071710184 vs 1795363456218301833)

Ninja would then rebuild edges that are already correct.  On the assembler that
means either a wasted full build or, worse, a build with no compiler available.

So we do the only thing that is honest here: walk the graph in dependency
order and bump each existing output to just past its newest input.  Nothing is
compiled and no content changes -- correctness rests on the sha256 verification
in `artifacts.merge`, not on timestamps.  Every bump is counted and reported so
the size of the lie is visible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import ninja

NUDGE_NS = 1_000_000  # 1 ms past the newest input; keeps ordering readable


@dataclass
class FreshenReport:
    considered: int = 0
    bumped: int = 0
    skipped_missing_output: int = 0
    skipped_missing_input: int = 0
    max_skew_ns: int = 0
    log_entries_rewritten: int = 0
    log_rewritten: bool = False
    missing_inputs: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "bumped": self.bumped,
            "skipped_missing_output": self.skipped_missing_output,
            "skipped_missing_input": self.skipped_missing_input,
            "max_skew_ns": self.max_skew_ns,
            "max_skew_s": round(self.max_skew_ns / 1e9, 3),
            "log_entries_rewritten": self.log_entries_rewritten,
            "log_rewritten": self.log_rewritten,
            "missing_inputs": self.missing_inputs[:50],
            "missing_inputs_count": len(self.missing_inputs),
            "dry_run": self.dry_run,
            "ok": self.ok,
        }

    @property
    def ok(self) -> bool:
        return not self.missing_inputs


def freshen(
    graph: ninja.Graph,
    root: str,
    dry_run: bool = False,
    log_path: str | None = None,
) -> FreshenReport:
    """Bump output mtimes so no edge looks stale. Metadata only."""
    report = FreshenReport(dry_run=dry_run)
    mtime: dict[str, int] = {}
    bumped: dict[str, int] = {}
    order = graph.topo()

    for idx in order:
        edge = graph.edges[idx]
        outs = [o for o in edge.outputs if os.path.exists(os.path.join(root, o))]
        if not outs:
            report.skipped_missing_output += len(edge.outputs)
            continue
        report.considered += 1

        newest_input = 0
        for dep in edge.all_inputs():
            full = os.path.join(root, dep)
            if dep in mtime:
                newest_input = max(newest_input, mtime[dep])
                continue
            if not os.path.exists(full):
                # Either a source file that was never synced, or an output no
                # worker owned. Both are worth naming.
                if dep not in graph.producer:
                    report.missing_inputs.append(dep)
                else:
                    report.skipped_missing_input += 1
                continue
            newest_input = max(newest_input, os.stat(full).st_mtime_ns)

        for out in outs:
            full = os.path.join(root, out)
            current = os.stat(full).st_mtime_ns
            if newest_input and current <= newest_input:
                skew = newest_input - current + NUDGE_NS
                report.max_skew_ns = max(report.max_skew_ns, skew)
                report.bumped += 1
                current = newest_input + NUDGE_NS
                bumped[out] = current
                if not dry_run:
                    os.utime(full, ns=(current, current))
            mtime[out] = current

    # Ninja trusts the log's mtime over the file's, so a bumped output also
    # needs its log entry rewritten.
    if bumped and not dry_run and log_path:
        report.log_rewritten = os.path.exists(log_path)
        report.log_entries_rewritten = ninja.rewrite_ninja_log(log_path, bumped)
    return report
