"""Decide which worker builds which edge.

Two things make this tractable:

  * every edge in a ninja graph has a place in the source tree (its inputs are
    either source files or other edges' outputs), so ownership can be
    propagated up from the leaves;
  * the partition only has to be *good*, not optimal -- ninja plus a shared
    compiler cache makes a mispredicted edge cost time, not correctness.

So: attribute every edge to a manifest project, group projects into sections,
shard the fat sections, hand shards to workers with LPT, then report exactly
how much work the split duplicates.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import ninja, sections

# Rule-name heuristics, used only when there is no .ninja_log to learn from.
# Values are "relative cost units", deliberately crude and clearly labelled in
# every report that uses them.
RULE_WEIGHT = [
    ("phony", 0.0),
    ("copy", 0.05),
    ("cp", 0.05),
    ("install", 0.05),
    ("symlink", 0.02),
    ("cc", 1.0),
    ("cxx", 1.0),
    ("clang", 1.0),
    ("gcc", 1.0),
    ("as", 0.4),
    ("link", 2.0),
    ("ar", 0.6),
    ("archive", 0.6),
    ("javac", 1.5),
    ("java", 1.5),
    ("d8", 3.0),
    ("r8", 3.0),
    ("dex", 3.0),
    ("protoc", 0.5),
    ("aidl", 0.3),
    ("gen", 0.5),
]


def rule_weight(rule: str) -> float:
    low = rule.lower()
    for needle, w in RULE_WEIGHT:
        if needle in low:
            return w
    return 0.2


class ProjectIndex:
    """Longest-prefix lookup from a source path to the manifest project."""

    def __init__(self, project_paths):
        self.paths = set(project_paths)
        self._cache: dict[str, str] = {}

    def lookup(self, path: str) -> str | None:
        hit = self._cache.get(path)
        if hit is not None:
            return hit if hit != "" else None
        parts = path.split("/")
        found = ""
        for i in range(len(parts), 0, -1):
            cand = "/".join(parts[:i])
            if cand in self.paths:
                found = cand
                break
        self._cache[path] = found
        return found or None


@dataclass
class Worker:
    id: str
    sections: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    edges: int = 0
    weight: float = 0.0
    closure_edges: int = 0
    closure_weight: float = 0.0
    targets: list[str] = field(default_factory=list)

    @property
    def duplication(self) -> float:
        if self.weight <= 0:
            return 0.0
        return (self.closure_weight - self.weight) / self.weight

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "sections": self.sections,
            "projects": self.projects,
            "edges": self.edges,
            "weight": round(self.weight, 3),
            "closure_edges": self.closure_edges,
            "closure_weight": round(self.closure_weight, 3),
            "duplication": round(self.duplication, 4),
        }


@dataclass
class Plan:
    workers: list[Worker]
    totals: dict
    digest: str = ""
    weights_source: str = ""

    def as_dict(self) -> dict:
        return {
            "digest": self.digest,
            "weights_source": self.weights_source,
            "workers": [w.as_dict() for w in self.workers],
            "totals": self.totals,
        }

    def write(self, directory: str) -> None:
        import os

        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "plan.json"), "w", encoding="utf-8") as fh:
            json.dump(self.as_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
        for w in self.workers:
            with open(os.path.join(directory, f"targets.{w.id}.txt"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(w.targets))
                fh.write("\n")


class Splitter:
    def __init__(
        self,
        graph: ninja.Graph,
        project_paths: list[str],
        policy: sections.Policy,
        workers: int,
        weights: dict[str, float] | None = None,
    ):
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self.graph = graph
        self.index = ProjectIndex(project_paths)
        self.policy = policy
        self.workers = workers
        self.ninja_log = weights or {}
        self.weights_source = "ninja_log" if weights else "rule_heuristic"
        self.order = graph.topo()
        self.edge_weight: list[float] = [0.0] * len(graph.edges)
        self.edge_owner: list[str] = [""] * len(graph.edges)

    # -- weights ---------------------------------------------------------

    def _weigh(self) -> None:
        for e in self.graph.edges:
            best = 0.0
            for out in e.outputs:
                best = max(best, self.ninja_log.get(out, 0.0))
            if best == 0.0:
                best = rule_weight(e.rule)
            self.edge_weight[e.index] = best

    # -- attribution -----------------------------------------------------

    def _attribute(self) -> None:
        """Edge -> project path, propagated up from source files."""
        for idx in self.order:
            e = self.graph.edges[idx]
            sources = [i for i in e.all_inputs() if i not in self.graph.producer]
            if sources:
                counts: Counter[str] = Counter()
                for s in sources:
                    proj = self.index.lookup(s)
                    if proj:
                        counts[proj] += 1
                if counts:
                    self.edge_owner[idx] = _pick(counts)
                    continue
            counts = Counter()
            for dep in e.all_inputs():
                j = self.graph.producer.get(dep)
                if j is not None and self.edge_owner[j]:
                    counts[self.edge_owner[j]] += 1
            self.edge_owner[idx] = _pick(counts) if counts else ""

    # -- sharding --------------------------------------------------------

    def _units(self) -> dict[str, list[str]]:
        """Section -> projects, split further when there are more workers."""
        project_weight: dict[str, float] = defaultdict(float)
        for e in self.graph.edges:
            owner = self.edge_owner[e.index]
            if owner:
                project_weight[owner] += self.edge_weight[e.index]

        by_section: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for proj, w in project_weight.items():
            by_section[self.policy.section_of(proj)].append((proj, w))
        for v in by_section.values():
            v.sort(key=lambda t: (-t[1], t[0]))

        units: dict[str, list[str]] = {}
        for sec, projects in by_section.items():
            units[sec] = [p for p, _ in projects]

        # Split the heaviest section until we have enough units to spread.
        while len(units) < self.workers:
            sec = max(units, key=lambda s: (self._unit_weight(units[s]), s))
            projs = units[sec]
            if len(projs) < 2:
                break
            half = self._split_projects(projs, project_weight)
            if not half:
                break
            del units[sec]
            for i, part in enumerate(half):
                units[f"{sec}.{i}"] = part
        return units

    def _split_projects(self, projects: list[str], project_weight: dict[str, float]):
        """Balanced two-way split, keeping the heaviest projects apart."""
        ordered = sorted(projects, key=lambda p: (-project_weight[p], p))
        if len(ordered) < 2:
            return []
        bins: list[list[str]] = [[], []]
        loads = [0.0, 0.0]
        for p in ordered:
            i = 0 if loads[0] <= loads[1] else 1
            bins[i].append(p)
            loads[i] += project_weight[p]
        if not bins[0] or not bins[1]:
            return []
        return [sorted(bins[0]), sorted(bins[1])]

    def _unit_weight(self, projects: list[str]) -> float:
        want = set(projects)
        return sum(
            self.edge_weight[e.index] for e in self.graph.edges
            if self.edge_owner[e.index] in want
        )

    # -- main ------------------------------------------------------------

    def run(self) -> Plan:
        self._weigh()
        self._attribute()

        units = self._units()
        unit_weight = {u: self._unit_weight(p) for u, p in units.items()}
        unit_section = {u: u.split(".")[0] for u in units}

        # LPT: biggest unit first, always to the currently lightest worker.
        order = sorted(units, key=lambda u: (-unit_weight[u], u))
        buckets: list[list[str]] = [[] for _ in range(self.workers)]
        loads = [0.0] * self.workers
        for u in order:
            i = min(range(self.workers), key=lambda k: (loads[k], k))
            buckets[i].append(u)
            loads[i] += unit_weight[u]

        unit_to_worker: dict[str, str] = {}
        workers: list[Worker] = []
        for i, units_i in enumerate(buckets):
            w = Worker(id=f"w{i}")
            w.sections = sorted({unit_section[u] for u in units_i})
            projects: list[str] = []
            for u in units_i:
                projects.extend(units[u])
                unit_to_worker[u] = w.id
            w.projects = sorted(set(projects))
            workers.append(w)

        owned: dict[str, list[int]] = defaultdict(list)
        self.edge_worker: list[str | None] = [None] * len(self.graph.edges)
        for e in self.graph.edges:
            owner = self.edge_owner[e.index]
            if not owner:
                continue
            sec = self.policy.section_of(owner)
            unit = self._unit_of(sec, owner, units)
            wid = unit_to_worker[unit]
            self.edge_worker[e.index] = wid
            owned[wid].append(e.index)

        goals = self.graph.goal_outputs()
        all_needed = self.graph.closure(goals)
        total_weight = sum(self.edge_weight[i] for i in all_needed)

        for w in workers:
            w.edges = len(owned[w.id])
            w.weight = sum(self.edge_weight[i] for i in owned[w.id])
            leaves = self._leaf_targets(owned[w.id])
            w.targets = sorted(leaves)
            close = self.graph.closure(leaves) if leaves else set()
            w.closure_edges = len(close)
            w.closure_weight = sum(self.edge_weight[i] for i in close)

        fleet_weight = sum(w.closure_weight for w in workers)
        serial = total_weight
        crit = self._critical_path(all_needed)
        unowned = len(all_needed) - sum(len(owned[w.id]) for w in workers)

        totals = {
            "edges_total": len(self.graph.edges),
            "edges_needed": len(all_needed),
            "edges_unowned": unowned,
            "weight_serial": round(serial, 3),
            "weight_critical_path": round(crit, 3),
            "weight_fleet": round(fleet_weight, 3),
            "duplication_factor": round(fleet_weight / serial, 4) if serial else 0.0,
            "ideal_speedup": round(serial / crit, 3) if crit else 0.0,
            "max_worker_weight": round(max((w.weight for w in workers), default=0.0), 3),
            "balance": round(
                (max((w.weight for w in workers), default=0.0) * len(workers)) / serial, 4
            ) if serial else 0.0,
            "units": len(units),
        }
        return Plan(workers=workers, totals=totals, weights_source=self.weights_source)

    def _unit_of(self, sec: str, project: str, units: dict[str, list[str]]) -> str:
        if sec in units and project in units[sec]:
            return sec
        for u, projs in units.items():
            if project in projs:
                return u
        # A project attributed to a section that never made it into units.
        raise KeyError(f"{project} ({sec}) is not in any unit")

    def _leaf_targets(self, edge_ids: list[int]) -> list[str]:
        """Outputs of these edges that no other owned edge consumes."""
        owned = set(edge_ids)
        consumed: set[str] = set()
        for i in owned:
            for dep in self.graph.edges[i].all_inputs():
                j = self.graph.producer.get(dep)
                if j in owned:
                    consumed.add(dep)
        out: list[str] = []
        for i in owned:
            for o in self.graph.edges[i].outputs:
                if o not in consumed:
                    out.append(o)
        return out

    def _critical_path(self, edge_ids: set[int]) -> float:
        """Longest weighted chain: the floor on wall time for any split."""
        dist: dict[int, float] = {}
        for idx in self.order:
            if idx not in edge_ids:
                continue
            best = 0.0
            for dep in self.graph.edges[idx].all_inputs():
                j = self.graph.producer.get(dep)
                if j is not None and j in dist:
                    best = max(best, dist[j])
            dist[idx] = best + self.edge_weight[idx]
        return max(dist.values(), default=0.0)


def _pick(counts: Counter) -> str:
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
