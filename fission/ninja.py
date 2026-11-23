"""A ninja file reader that is just good enough to rebuild the build graph.

We never execute ninja's commands ourselves -- we hand target lists back to
ninja.  What we need is the dependency graph: which output is produced by
which edge, from which inputs, with which rule, and how long it took last
time (from .ninja_log).

Handles the syntax AOSP actually emits: `subninja`, `include`, `$`
continuations, `$$` / `$ ` / `$:` escapes, multiple outputs, implicit (`|`)
and order-only (`||`) inputs, pools, and per-edge bindings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class NinjaError(RuntimeError):
    pass


@dataclass
class Edge:
    outputs: list[str]
    rule: str
    inputs: list[str] = field(default_factory=list)
    implicit: list[str] = field(default_factory=list)
    order_only: list[str] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)
    origin: str = ""
    index: int = -1

    def all_inputs(self) -> list[str]:
        return self.inputs + self.implicit


@dataclass
class Graph:
    edges: list[Edge] = field(default_factory=list)
    producer: dict[str, int] = field(default_factory=dict)  # output path -> edge index
    defaults: list[str] = field(default_factory=list)
    rules: dict[str, dict[str, str]] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    # -- construction ----------------------------------------------------

    def add(self, edge: Edge) -> None:
        edge.index = len(self.edges)
        self.edges.append(edge)
        for out in edge.outputs:
            if out in self.producer:
                prev = self.edges[self.producer[out]]
                raise NinjaError(
                    f"{out} is produced twice: {prev.rule} at {prev.origin} "
                    f"and {edge.rule} at {edge.origin}"
                )
            self.producer[out] = edge.index

    # -- queries ---------------------------------------------------------

    def topo(self) -> list[int]:
        """Edge indices in dependency order (Kahn)."""
        indeg = [0] * len(self.edges)
        users: list[list[int]] = [[] for _ in self.edges]
        for e in self.edges:
            for dep in e.all_inputs():
                j = self.producer.get(dep)
                if j is not None:
                    indeg[e.index] += 1
                    users[j].append(e.index)
        ready = [i for i, d in enumerate(indeg) if d == 0]
        order: list[int] = []
        while ready:
            i = ready.pop()
            order.append(i)
            for u in users[i]:
                indeg[u] -= 1
                if indeg[u] == 0:
                    ready.append(u)
        if len(order) != len(self.edges):
            stuck = [self.edges[i].outputs[0] for i, d in enumerate(indeg) if d]
            raise NinjaError(f"dependency cycle among {len(stuck)} edges, e.g. {stuck[:5]}")
        return order

    def closure(self, goals: list[str]) -> set[int]:
        """Every edge needed to build `goals`."""
        seen: set[int] = set()
        stack: list[int] = []
        for g in goals:
            idx = self.producer.get(g)
            if idx is None:
                raise NinjaError(f"no edge produces {g!r}")
            stack.append(idx)
        while stack:
            i = stack.pop()
            if i in seen:
                continue
            seen.add(i)
            for dep in self.edges[i].all_inputs():
                j = self.producer.get(dep)
                if j is not None and j not in seen:
                    stack.append(j)
        return seen

    def goal_outputs(self) -> list[str]:
        """Outputs nothing else consumes: the natural build goals."""
        consumed = set()
        for e in self.edges:
            consumed.update(e.all_inputs())
        out = []
        for e in self.edges:
            for o in e.outputs:
                if o not in consumed:
                    out.append(o)
        return out


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _logical_lines(text: str):
    buf = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        trailing = len(stripped) - len(stripped.rstrip("$"))
        if trailing % 2 == 1:  # odd number of $ at end -> continuation
            buf += stripped[:-1]
            continue
        buf += raw
        yield buf
        buf = ""


def _lex(line: str) -> list[str]:
    """Split one build line, honouring ninja's escapes."""
    toks: list[str] = []
    cur: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "$" and i + 1 < n:
            nxt = line[i + 1]
            if nxt == "$":
                cur.append("$")
                i += 2
                continue
            if nxt == " ":
                cur.append(" ")
                i += 2
                continue
            if nxt == ":":
                cur.append(":")
                i += 2
                continue
            if nxt == "{":
                end = line.find("}", i + 2)
                if end < 0:
                    raise NinjaError(f"unterminated ${{ in: {line[:120]}")
                cur.append(line[i:end + 1])
                i = end + 1
                continue
            cur.append(c)
            i += 1
            continue
        if c in " \t":
            if cur:
                toks.append("".join(cur))
                cur = []
            i += 1
            continue
        if c == ":":
            if cur:
                toks.append("".join(cur))
                cur = []
            toks.append(":")
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        toks.append("".join(cur))
    return toks


_VAR_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def _expand(s: str, scope: dict[str, str], depth: int = 0) -> str:
    if "$" not in s:
        return s
    if depth > 8:
        return s
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "$":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            i += 1
            continue
        nxt = s[i + 1]
        if nxt == "$":
            out.append("$")
            i += 2
            continue
        if nxt == " ":
            out.append(" ")
            i += 2
            continue
        if nxt == ":":
            out.append(":")
            i += 2
            continue
        if nxt == "{":
            end = s.find("}", i + 2)
            if end < 0:
                out.append(s[i:])
                break
            name = s[i + 2:end]
            out.append(_expand(scope.get(name, ""), scope, depth + 1))
            i = end + 1
            continue
        j = i + 1
        while j < n and s[j] in _VAR_CHARS:
            j += 1
        name = s[i + 1:j]
        out.append(_expand(scope.get(name, ""), scope, depth + 1))
        i = j
    return "".join(out)


def _parse(path: str, graph: Graph, scope: dict[str, str], seen: set[str]) -> None:
    real = os.path.realpath(path)
    if real in seen:
        return
    seen.add(real)
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = list(_logical_lines(fh.read()))
    graph.files.append(path)

    local = dict(scope)
    pending: Edge | None = None
    current_rule: str | None = None

    for line in lines:
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        indented = line[0] in " \t"
        if indented:
            body = line.strip()
            if "=" not in body:
                continue
            key, _, val = body.partition("=")
            key = key.strip()
            val = _expand(val.strip(), local)
            if pending is not None:
                pending.bindings[key] = val
            elif current_rule is not None:
                graph.rules.setdefault(current_rule, {})[key] = val
            continue

        pending = None
        current_rule = None
        stripped = line.strip()

        if stripped.startswith("build "):
            toks = _lex(stripped[len("build "):])
            if ":" not in toks:
                raise NinjaError(f"{path}: build line without ':': {stripped[:120]}")
            colon = toks.index(":")
            outs = [_expand(t, local) for t in toks[:colon]]
            rest = toks[colon + 1:]
            if not rest:
                raise NinjaError(f"{path}: build line without a rule: {stripped[:120]}")
            rule = rest[0]
            mode = "explicit"
            ins: list[str] = []
            imp: list[str] = []
            oo: list[str] = []
            for t in rest[1:]:
                if t == "|":
                    mode = "implicit"
                    continue
                if t == "||":
                    mode = "order_only"
                    continue
                val = _expand(t, local)
                if mode == "explicit":
                    ins.append(val)
                elif mode == "implicit":
                    imp.append(val)
                else:
                    oo.append(val)
            e = Edge(outputs=outs, rule=rule, inputs=ins, implicit=imp, order_only=oo, origin=path)
            graph.add(e)
            pending = e
            continue

        if stripped.startswith("default "):
            toks = _lex(stripped[len("default "):])
            graph.defaults.extend(_expand(t, local) for t in toks if t != ":")
            continue

        if stripped.startswith("subninja ") or stripped.startswith("include "):
            kw, _, arg = stripped.partition(" ")
            target = _expand(arg.strip(), local)
            resolved = _resolve_include(target, path)
            if kw == "include":
                _parse(resolved, graph, local, seen)
            else:
                _parse(resolved, graph, dict(local), seen)
            continue

        if stripped.startswith("rule "):
            current_rule = stripped[len("rule "):].strip()
            graph.rules.setdefault(current_rule, {})
            continue

        if stripped.startswith("pool ") or stripped.startswith("builddir"):
            continue

        if "=" in stripped:
            key, _, val = stripped.partition("=")
            local[key.strip()] = _expand(val.strip(), local)
            continue
        # anything else (e.g. a stray comment form) is ignored on purpose


def _resolve_include(target: str, including_file: str) -> str:
    if os.path.isabs(target):
        return target
    for base in (os.path.dirname(including_file), os.getcwd()):
        cand = os.path.join(base, target) if base else target
        if os.path.exists(cand):
            return cand
    raise NinjaError(f"{including_file}: cannot resolve include {target!r}")


def load(paths: list[str], root: str | None = None) -> Graph:
    """Load one or more ninja files (a combined.ninja pulls in soong's).

    `root` is the source tree; ninja paths are relative to it, so we chdir for
    the duration of the parse and put the process back where we found it.
    """
    old = os.getcwd()
    if root:
        os.chdir(root)
    try:
        g = Graph()
        seen: set[str] = set()
        for p in paths:
            if not os.path.exists(p):
                raise NinjaError(f"no such ninja file: {p}")
            _parse(p, g, {}, seen)
    finally:
        os.chdir(old)
    if not g.edges:
        raise NinjaError("parsed zero build edges -- is this the real combined.ninja?")
    return g


def load_ninja_log(path: str) -> dict[str, float]:
    """Output path -> wall seconds, from .ninja_log (v5+)."""
    if not os.path.exists(path):
        return {}
    out: dict[str, float] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            out[parts[3]] = max(0.0, (end - start) / 1000.0)
    return out


def rewrite_ninja_log(path: str, mtime_by_output: dict[str, int]) -> int:
    """Update recorded mtimes in a .ninja_log for outputs we just re-stamped.

    Ninja's staleness check reads the *log's* mtime for an output when an entry
    exists, so bumping the file's mtime without touching the log changes
    nothing. Returns the number of entries rewritten.
    """
    if not os.path.exists(path) or not mtime_by_output:
        return 0
    lines: list[str] = []
    changed = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                lines.append(line)
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                lines.append(line)
                continue
            out = parts[3]
            if out in mtime_by_output:
                parts[2] = str(mtime_by_output[out])
                changed += 1
            lines.append("\t".join(parts) + "\n")
    tmp = path + ".fission-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.replace(tmp, path)
    return changed
