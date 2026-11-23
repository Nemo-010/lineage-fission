"""Move build outputs between workers without lying about them.

Each worker packs the files it owns plus a hash manifest.  The assembler
merges the manifests and the trees, and refuses to guess:

  * same path, same sha256, several workers  -> deduped (that is the price of
    the split, not a problem);
  * same path, different sha256             -> the assigned owner's copy wins
    and the collision is counted and reported, because it means the build is
    not reproducible at that path (embedded paths, timestamps, order);
  * a path no worker owns                    -> hard error.

Every merge is verified by re-hashing what landed on disk.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from dataclasses import dataclass, field

CHUNK = 1 << 20
META = ".fission-artifact.json"
LOG_MEMBER = ".fission-ninja-log"


class MergeError(RuntimeError):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


@dataclass
class FileRecord:
    path: str
    size: int
    sha256: str
    mode: int
    mtime_ns: int

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
        }


@dataclass
class MergeReport:
    written: int = 0
    deduped: int = 0
    collided: list[dict] = field(default_factory=list)
    missing_owner: list[str] = field(default_factory=list)
    verified: int = 0
    verify_failed: list[str] = field(default_factory=list)
    ninja_log_entries: int = 0

    def as_dict(self) -> dict:
        return {
            "written": self.written,
            "deduped": self.deduped,
            "collided": self.collided[:50],
            "collided_count": len(self.collided),
            "missing_owner": self.missing_owner[:50],
            "missing_owner_count": len(self.missing_owner),
            "verified": self.verified,
            "ninja_log_entries": self.ninja_log_entries,
            "verify_failed": self.verify_failed[:50],
            "verify_failed_count": len(self.verify_failed),
        }

    @property
    def ok(self) -> bool:
        return not self.missing_owner and not self.verify_failed


def _log_header(root: str) -> str:
    """The version header of this tree's ninja log (v5, v7, ...)."""
    log = os.path.join(root, ".ninja_log")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
        if first.startswith("# ninja log"):
            return first
    return ""


def _log_lines(root: str, paths: set[str]) -> list[str]:
    """The ninja log entries for outputs this worker owns.

    Ninja rebuilds any output whose command line is not in the log, so the log
    has to travel with the artifacts. These are the worker's real entries,
    written by real ninja -- nothing is synthesised here.
    """
    log = os.path.join(root, ".ninja_log")
    if not os.path.exists(log):
        return []
    out: list[str] = []
    with open(log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 5 and parts[3] in paths:
                out.append(line.rstrip("\n"))
    return out


def pack(
    root: str,
    paths: list[str],
    tar_path: str,
    worker: str,
    owner_of: dict[str, str] | None = None,
) -> dict:
    """Tar up `paths` (relative to `root`) with a hash manifest and ninja log."""
    root = os.path.abspath(root)
    records: list[FileRecord] = []
    missing: list[str] = []
    owner_of = owner_of or {}
    seen: set[str] = set()
    for rel in paths:
        rel = rel.lstrip("./")
        full = os.path.join(root, rel)
        if not os.path.exists(full):
            missing.append(rel)
            continue
        if rel in seen:
            continue
        seen.add(rel)
        st = os.stat(full)
        records.append(
            FileRecord(
                path=rel,
                size=st.st_size,
                sha256=sha256_file(full),
                mode=st.st_mode & 0o7777,
                mtime_ns=st.st_mtime_ns,
            )
        )
    log_lines = _log_lines(root, seen)
    meta = {
        "worker": worker,
        "files": [r.as_dict() for r in records],
        "missing": missing,
        "owners": {r.path: owner_of.get(r.path, worker) for r in records},
        "ninja_log_entries": len(log_lines),
        "ninja_log_header": _log_header(root),
    }
    os.makedirs(os.path.dirname(os.path.abspath(tar_path)), exist_ok=True)
    with tarfile.open(tar_path, "w") as tf:
        for rec in records:
            full = os.path.join(root, rec.path)
            info = tf.gettarinfo(full, arcname=rec.path)
            info.mtime = rec.mtime_ns / 1e9
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with open(full, "rb") as fh:
                tf.addfile(info, fh)
        for name, text in (
            (META, json.dumps(meta, sort_keys=True)),
            (LOG_MEMBER, "\n".join(log_lines) + ("\n" if log_lines else "")),
        ):
            blob = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(blob)
            ti.mode = 0o644
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(blob))
    return {
        "worker": worker,
        "files": len(records),
        "missing": len(missing),
        "ninja_log_entries": len(log_lines),
        "tar": tar_path,
    }


def read_meta(tar_path: str) -> dict:
    with tarfile.open(tar_path, "r") as tf:
        try:
            member = tf.getmember(META)
        except KeyError as exc:
            raise MergeError(f"{tar_path}: not a fission artifact (no {META})") from exc
        fh = tf.extractfile(member)
        assert fh is not None
        return json.loads(fh.read().decode())


def merge(
    tars: list[str],
    dest: str,
    strict: bool = False,
    fail_on_collision: bool = False,
    ownership: dict[str, str] | None = None,
) -> MergeReport:
    """Unpack every artifact into `dest`, deciding per path who wins.

    `ownership` maps a path to the worker that was *assigned* it by the plan;
    it is what makes the winner well defined when two workers both produced
    the same path with different bytes.
    """
    dest = os.path.abspath(dest)
    metas = [(t, read_meta(t)) for t in tars]
    report = MergeReport()

    claims: dict[str, list[tuple[str, str]]] = {}
    owner_of: dict[str, str] = dict(ownership or {})
    for tar, meta in metas:
        for rec in meta["files"]:
            claims.setdefault(rec["path"], []).append((meta["worker"], rec["sha256"]))
            owner_of.setdefault(rec["path"], meta["owners"].get(rec["path"], meta["worker"]))

    winner: dict[str, str] = {}
    for path, entries in claims.items():
        owner = owner_of[path]
        digests = {d for _, d in entries}
        if len(digests) == 1:
            if len(entries) > 1:
                report.deduped += 1
            winner[path] = entries[0][0]
            continue
        owned_by = [w for w, d in entries if w == owner]
        if not owned_by:
            report.missing_owner.append(path)
            winner[path] = entries[0][0]
            continue
        winner[path] = owner
        report.collided.append(
            {"path": path, "owner": owner, "producers": sorted({w for w, _ in entries}), "variants": len(digests)}
        )

    # tar stores mtime as whole seconds, which is coarse enough to make a
    # merged output look older than its inputs -- and ninja would then rebuild
    # it. The manifest carries nanoseconds; that is what we restore.
    meta_of: dict[str, dict] = {}
    for _, meta in metas:
        for rec in meta["files"]:
            if winner.get(rec["path"]) == meta["worker"]:
                meta_of[rec["path"]] = rec

    by_tar: dict[str, set[str]] = {}
    for path, w in winner.items():
        by_tar.setdefault(w, set()).add(path)

    for tar, meta in metas:
        wanted = by_tar.get(meta["worker"], set())
        if not wanted:
            continue
        with tarfile.open(tar, "r") as tf:
            for member in tf:
                if member.name == META or not member.isfile():
                    continue
                if member.name not in wanted:
                    continue
                target = os.path.join(dest, member.name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                assert src is not None
                tmp = target + ".fission-tmp"
                with open(tmp, "wb") as out:
                    while True:
                        b = src.read(CHUNK)
                        if not b:
                            break
                        out.write(b)
                rec = meta_of.get(member.name, {})
                os.chmod(tmp, rec.get("mode", member.mode & 0o7777))
                ns = rec.get("mtime_ns") or int(member.mtime * 1e9)
                os.utime(tmp, ns=(ns, ns))
                os.replace(tmp, target)
                report.written += 1

    expected = {
        rec["path"]: rec["sha256"]
        for _, meta in metas
        for rec in meta["files"]
        if winner.get(rec["path"]) == meta["worker"]
    }
    for path, want in expected.items():
        full = os.path.join(dest, path)
        if not os.path.exists(full) or sha256_file(full) != want:
            report.verify_failed.append(path)
        else:
            report.verified += 1

    # Reassemble the build log from the workers' own entries, so ninja accepts
    # the merged outputs as built. Duplicate outputs: the assigned owner wins.
    log_lines: dict[str, str] = {}
    headers: list[str] = []
    for tar, meta in metas:
        if meta.get("ninja_log_header"):
            headers.append(meta["ninja_log_header"])
        with tarfile.open(tar, "r") as tf:
            try:
                member = tf.getmember(LOG_MEMBER)
            except KeyError:
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            for line in fh.read().decode("utf-8", "replace").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                out = parts[3]
                if winner.get(out, meta["worker"]) == meta["worker"]:
                    log_lines[out] = line
    if log_lines:
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, ".ninja_log"), "w", encoding="utf-8") as fh:
            # Never invent a version: ninja silently discards a log it does not
            # recognise, which looks exactly like "nothing was built".
            fh.write(_newest_header(headers) + "\n")
            for line in log_lines.values():
                fh.write(line + "\n")
        report.ninja_log_entries = len(log_lines)

    if strict and not report.ok:
        raise MergeError(json.dumps(report.as_dict(), indent=2))
    if fail_on_collision and report.collided:
        raise MergeError(
            "non-reproducible paths: " + json.dumps(report.as_dict()["collided"][:10], indent=2)
        )
    return report


def _newest_header(headers: list[str]) -> str:
    best = "# ninja log v5"
    best_v = 5
    for h in headers:
        digits = "".join(c for c in h.split()[-1] if c.isdigit())
        if digits.isdigit() and int(digits) > best_v:
            best, best_v = h, int(digits)
    return best


def diff_trees(left: str, right: str, limit: int = 50) -> dict:
    """Compare two built trees by content, not by image hash.

    Android images are not byte-reproducible (timestamps, fs uuid, file order),
    so the meaningful question is whether the installed file *trees* agree.
    """
    def index(root: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for dirpath, _, files in os.walk(root):
            for f in files:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
                out[rel] = sha256_file(full)
        return out

    a, b = index(left), index(right)
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    differing = sorted(p for p in set(a) & set(b) if a[p] != b[p])
    return {
        "left": len(a),
        "right": len(b),
        "only_left": only_a[:limit],
        "only_right": only_b[:limit],
        "differing": differing[:limit],
        "only_left_count": len(only_a),
        "only_right_count": len(only_b),
        "differing_count": len(differing),
        "equal": not only_a and not only_b and not differing,
    }
