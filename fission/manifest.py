"""Parse repo manifests the way `repo` does, far enough to answer one question:

    which projects does this device actually need, and where does each one land?

The three manifest sources that matter for a LineageOS build are:

  * the upstream manifest (LineageOS/android:default.xml) -- ~1300 projects
  * .repo/local_manifests/*.xml -- user overrides, and what `breakfast` writes
  * a device manifest (TheMuppets/manifests:muppets.xml, or a hand written
    local manifest) -- the vendor blobs, selected by the device's repo group

plus `lineage.dependencies`, the JSON file each LineageOS device tree carries,
which is what makes a device tree pull in its common tree and its kernel.

Only stdlib. Nothing here touches the network unless you ask for a reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator


class ManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Remote:
    name: str
    fetch: str
    revision: str | None = None
    review: str | None = None


@dataclass
class Project:
    name: str
    path: str
    remote: str
    revision: str
    groups: frozenset[str] = frozenset()
    clone_depth: int | None = None
    copyfiles: tuple[tuple[str, str], ...] = ()
    linkfiles: tuple[tuple[str, str], ...] = ()
    origin: str = ""
    url: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "remote": self.remote,
            "revision": self.revision,
            "groups": sorted(self.groups),
            "clone_depth": self.clone_depth,
            "url": self.url,
            "origin": self.origin,
        }


@dataclass
class Manifest:
    remotes: dict[str, Remote] = field(default_factory=dict)
    default_revision: str = ""
    default_remote: str = ""
    default_groups: frozenset[str] = frozenset()
    projects: dict[str, Project] = field(default_factory=dict)  # path -> Project
    sources: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    manifest_url: str = ""

    # -- queries ---------------------------------------------------------

    def by_name(self, name: str) -> Project | None:
        for p in self.projects.values():
            if p.name == name:
                return p
        return None

    def group_names(self) -> set[str]:
        out: set[str] = set()
        for p in self.projects.values():
            out |= set(p.groups)
        return out

    def device_groups(self, device: str) -> list[str]:
        """Groups that look like they were cut for this device."""
        wanted = {f"muppets_{device}"}
        return sorted(g for g in self.group_names() if g in wanted)

    def select(self, groups: Iterable[str] = (), exclude: Iterable[str] = ()) -> "Manifest":
        """Apply repo's group filter.

        repo's rule: a project with no groups is always in; a project with
        groups is in only if its groups intersect the enabled set (the
        manifest's <manifest groups=...> plus whatever -g added). A project is
        out if any of its groups is explicitly excluded.
        """
        enabled = set(self.default_groups) | set(groups)
        excluded = set(exclude)
        kept: dict[str, Project] = {}
        for path, p in self.projects.items():
            if p.groups & excluded:
                continue
            if p.groups and not (p.groups & enabled):
                continue
            kept[path] = p
        return replace(self, projects=kept)

    def prune_device(self, device: str) -> tuple[int, list[str]]:
        """Drop vendor trees that belong to other devices.

        TheMuppets/manifests:muppets.xml lists ~250 vendor trees, all grouped
        `muppets,muppets_<codename>`, and none of them carry `notdefault`. That
        matters: repo's MatchesGroups() adds `default` to any project without
        `notdefault`, so a plain `repo sync` takes *every* one of them, and
        `-g muppets_<codename>` would drop every pdk project at the same time.
        There is no group selection that yields exactly one device, which is
        why the plan prunes by group here instead of trusting repo's filter.
        """
        want = f"muppets_{device}"
        keep: dict[str, Project] = {}
        dropped: list[str] = []
        for path, p in self.projects.items():
            vendor_groups = {g for g in p.groups if g.startswith("muppets_")}
            if vendor_groups and want not in p.groups:
                dropped.append(path)
                continue
            keep[path] = p
        self.projects = keep
        return len(dropped), dropped

    def add_project(
        self,
        name: str,
        path: str,
        revision: str | None = None,
        remote: str | None = None,
        origin: str = "added",
    ) -> Project:
        remote_name = remote or self.default_remote
        r = self.remotes.get(remote_name)
        if r is None:
            raise ManifestError(f"cannot add {name}: unknown remote {remote_name!r}")
        proj = Project(
            name=name,
            path=path,
            remote=remote_name,
            revision=revision or r.revision or self.default_revision,
            origin=origin,
            url=project_url(r, name, self.manifest_url),
        )
        self.projects[path] = proj
        return proj

    def digest(self) -> str:
        """Stable hash of the resolved project set: the cache key for source."""
        h = hashlib.sha256()
        for path in sorted(self.projects):
            p = self.projects[path]
            h.update(f"{path}\0{p.name}\0{p.revision}\0{p.url}\n".encode())
        return h.hexdigest()

    def total_clone_depth(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.projects.values():
            if p.clone_depth:
                out[p.path] = p.clone_depth
        return out


# --------------------------------------------------------------------------
# URL resolution, following repo's rules
# --------------------------------------------------------------------------

def resolve_fetch(base_url: str, fetch: str) -> str:
    """Turn a <remote fetch=...> into an absolute URL, the way repo does.

    repo uses urllib.parse.urljoin against the *manifest repository's* URL,
    which treats that URL as a file: `fetch=".."` from
    https://github.com/LineageOS/android lands on https://github.com/ , and the
    project name (already "LineageOS/android_build") is appended after. Getting
    this wrong silently yields URLs like .../LineageOS/LineageOS/android_build
    that 404, and every fetch quietly returns nothing.
    """
    fetch = fetch.strip()
    if "://" in fetch or fetch.startswith("git@") or fetch.startswith("ssh://"):
        return fetch.rstrip("/")
    base = base_url.strip().rstrip("/")
    if not base:
        raise ManifestError(f"cannot resolve relative fetch {fetch!r} without a manifest url")
    if fetch.startswith("/"):
        scheme, _, rest = base.partition("://")
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}{fetch.rstrip('/')}"
    resolved = urllib.parse.urljoin(base, fetch)
    return resolved.rstrip("/")


def project_url(remote: Remote, name: str, base_url: str) -> str:
    return resolve_fetch(base_url, remote.fetch) + "/" + name


# --------------------------------------------------------------------------
# XML parsing
# --------------------------------------------------------------------------

def _attr_groups(node: ET.Element) -> frozenset[str]:
    raw = node.get("groups", "")
    return frozenset(g for g in raw.split(",") if g)


@dataclass
class _Ctx:
    """Where to look for <include> targets, and whether to go to the network."""

    manifest_url: str = ""
    include_dirs: tuple[str, ...] = ()
    fetch_includes: bool = False
    cache_dir: str | None = None
    fetched: list[str] = field(default_factory=list)


def _fetch_include(name: str, manifest: "Manifest", ctx: _Ctx) -> str:
    """Pull an included manifest out of the manifest repository itself."""
    import base64
    import urllib.error
    import urllib.request

    base = ctx.manifest_url.rstrip("/")
    if not base:
        raise ManifestError(f"<include name={name!r}> needs --manifest-url to fetch")
    rev = manifest.default_revision or "HEAD"
    for prefix in ("refs/heads/", "refs/tags/"):
        if rev.startswith(prefix):
            rev = rev[len(prefix):]
    if base.startswith("https://github.com/"):
        url = f"{base}/raw/{rev}/{name}"
    elif base.startswith("https://android.googlesource.com/"):
        url = f"{base}/+/{rev}/{name}?format=TEXT"
    else:
        raise ManifestError(f"don't know how to fetch includes from {base}")
    req = urllib.request.Request(url, headers={"User-Agent": "lineage-fission"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise ManifestError(f"include {name}: {url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ManifestError(f"include {name}: {url} -> {exc.reason}") from exc
    if "googlesource.com" in url:
        body = base64.b64decode(body)
    cache = ctx.cache_dir or os.path.join(os.getcwd(), ".fission-includes")
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, name.replace("/", "__"))
    with open(dest, "wb") as fh:
        fh.write(body)
    ctx.fetched.append(dest)
    return dest


def _parse_file(path: str, manifest: Manifest, ctx: _Ctx) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    if root.tag != "manifest":
        raise ManifestError(f"{path}: root element is <{root.tag}>, expected <manifest>")

    for node in root:
        tag = node.tag
        if tag == "remote":
            name = node.get("name")
            if not name:
                raise ManifestError(f"{path}: <remote> without name")
            manifest.remotes[name] = Remote(
                name=name,
                fetch=node.get("fetch", ""),
                revision=node.get("revision"),
                review=node.get("review"),
            )
        elif tag == "default":
            manifest.default_revision = node.get("revision", manifest.default_revision)
            manifest.default_remote = node.get("remote", manifest.default_remote)
            manifest.default_groups = _attr_groups(node) | manifest.default_groups
        elif tag == "project":
            name = node.get("name")
            if not name:
                raise ManifestError(f"{path}: <project> without name")
            path_ = node.get("path") or name
            remote_name = node.get("remote") or manifest.default_remote
            remote = manifest.remotes.get(remote_name)
            if remote is None:
                raise ManifestError(f"{path}: project {name} uses unknown remote {remote_name!r}")
            revision = node.get("revision") or remote.revision or manifest.default_revision
            if not revision:
                raise ManifestError(f"{path}: project {name} has no revision and no default")
            depth = node.get("clone-depth")
            manifest.projects[path_] = Project(
                name=name,
                path=path_,
                remote=remote_name,
                revision=revision,
                groups=_attr_groups(node),
                clone_depth=int(depth) if depth else None,
                copyfiles=tuple((c.get("src", ""), c.get("dest", "")) for c in node.findall("copyfile")),
                linkfiles=tuple((c.get("src", ""), c.get("dest", "")) for c in node.findall("linkfile")),
                origin=path,
                url=project_url(remote, name, ctx.manifest_url),
            )
        elif tag == "remove-project":
            name = node.get("name")
            path_ = node.get("path")
            optional = node.get("optional") == "true"
            victims = [
                p for p in manifest.projects.values()
                if (name is None or p.name == name) and (path_ is None or p.path == path_)
            ]
            if not victims and not optional:
                raise ManifestError(f"{path}: remove-project {name or path_} matched nothing")
            for v in victims:
                del manifest.projects[v.path]
        elif tag == "extend-project":
            name = node.get("name")
            path_ = node.get("path")
            hits = [
                p for p in manifest.projects.values()
                if (name is None or p.name == name) and (path_ is None or p.path == path_)
            ]
            if not hits:
                raise ManifestError(f"{path}: extend-project {name or path_} matched nothing")
            for p in hits:
                p.groups = p.groups | _attr_groups(node)
                if node.get("revision"):
                    p.revision = node.get("revision")
        elif tag in ("include", "superproject", "contactinfo", "repo-hooks", "notice"):
            if tag == "include":
                # repo resolves <include> against the manifest repository itself.
                target = node.get("name", "")
                candidates = [os.path.join(os.path.dirname(path), target)]
                candidates += [os.path.join(d, target) for d in ctx.include_dirs]
                found = next((c for c in candidates if os.path.exists(c)), None)
                if found is None and ctx.fetch_includes:
                    found = _fetch_include(target, manifest, ctx)
                if found is None:
                    raise ManifestError(
                        f"{path}: <include name={target!r}> is not on disk. "
                        f"Pass --include-dir, or --fetch-includes to pull it from the "
                        f"manifest repository (repo does this for you)."
                    )
                manifest.includes.append(found)
                _parse_file(found, manifest, ctx)
        else:
            raise ManifestError(f"{path}: unknown manifest element <{tag}>")


def parse(
    files: Iterable[str],
    manifest_url: str = "",
    include_dirs: Iterable[str] = (),
    fetch_includes: bool = False,
    cache_dir: str | None = None,
) -> Manifest:
    """Parse manifest files in order, applying repo's overlay semantics."""
    m = Manifest(manifest_url=manifest_url)
    ctx = _Ctx(
        manifest_url=manifest_url,
        include_dirs=tuple(include_dirs),
        fetch_includes=fetch_includes,
        cache_dir=cache_dir,
    )
    for f in files:
        if not os.path.exists(f):
            raise ManifestError(f"no such manifest file: {f}")
        _parse_file(f, m, ctx)
        m.sources.append(f)
    m.sources.extend(ctx.fetched)
    if not m.projects:
        raise ManifestError("manifest set resolved to zero projects")
    return m


# --------------------------------------------------------------------------
# lineage.dependencies -- the device manifest's own manifest
# --------------------------------------------------------------------------

class TreeReader:
    """Read files out of an already-synced tree."""

    def __init__(self, root: str):
        self.root = root

    def read(self, project, filename: str) -> str | None:
        path = project.path if hasattr(project, "path") else str(project)
        full = os.path.join(self.root, path, filename)
        if not os.path.exists(full):
            return None
        with open(full, encoding="utf-8") as fh:
            return fh.read()


class HttpReader:
    """Read a file straight out of a project's git host (raw HTTP)."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def read(self, project: "Project", filename: str) -> str | None:
        import time
        import urllib.error
        import urllib.request

        url = _raw_url(project, filename)
        if url is None:
            return None
        delay = 1.0
        for attempt in range(4):
            req = urllib.request.Request(url, headers={"User-Agent": "lineage-fission"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ManifestError(f"{url}: HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ManifestError(f"{url}: {exc.reason}") from exc
        return None


def _raw_url(project: Project, filename: str) -> str | None:
    url = project.url.rstrip("/")
    rev = project.revision
    for prefix in ("refs/heads/", "refs/tags/"):
        if rev.startswith(prefix):
            rev = rev[len(prefix):]
    if url.startswith("https://github.com/"):
        return f"{url}/raw/{rev}/{filename}"
    if url.startswith("https://android.googlesource.com/"):
        return f"{url}/+/{rev}/{filename}?format=TEXT"
    return None


def _decode(text: str, project: Project) -> str:
    if "googlesource.com" in project.url:
        import base64

        try:
            return base64.b64decode(text).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise ManifestError(f"{project.name}: googlesource payload is not base64") from exc
    return text


def expand_dependencies(
    manifest: Manifest,
    reader,
    roots: Iterable[str] | None = None,
    max_depth: int = 6,
) -> Manifest:
    """Follow `lineage.dependencies` from every project, the way breakfast does.

    redfin -> device/google/redbull -> device/google/gs-common + the kernel.
    A device tree is never just one repository, and a build plan that pretends
    otherwise will be missing half its inputs.
    """
    added = 0
    if roots is None:
        # lineage.dependencies is a device-tree file. Fetching it for all 1400
        # projects would be 1400 requests to fetch 60 answers (and rate limits).
        frontier = sorted(
            (
                p for p in manifest.projects.values()
                if p.path.startswith(("device/", "vendor/"))
            ),
            key=lambda p: p.path,
        )
    else:
        wanted = set(roots)
        frontier = sorted(
            (p for p in manifest.projects.values() if p.path in wanted), key=lambda p: p.path
        )
    depth = 0
    seen = {p.path for p in manifest.projects.values()}
    while frontier and depth < max_depth:
        depth += 1
        nxt: list[Project] = []
        for proj in frontier:
            raw = reader.read(proj, "lineage.dependencies")
            if not raw:
                continue
            raw = _decode(raw, proj)
            try:
                deps = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ManifestError(f"{proj.path}/lineage.dependencies: {exc}") from exc
            owner = proj.name.split("/")[0] if "/" in proj.name else proj.name
            for dep in deps:
                repo = dep.get("repository")
                target = dep.get("target_path")
                if not repo or not target:
                    raise ManifestError(
                        f"{proj.path}/lineage.dependencies: entry needs repository + target_path"
                    )
                if target in seen:
                    continue
                remote_name = dep.get("remote") or proj.remote
                remote = manifest.remotes.get(remote_name)
                if remote is None:
                    raise ManifestError(f"{proj.path}: dependency {repo} uses unknown remote {remote_name}")
                name = repo if "/" in repo else f"{owner}/{repo}"
                revision = dep.get("branch") or proj.revision
                new = Project(
                    name=name,
                    path=target,
                    remote=remote_name,
                    revision=revision,
                    groups=proj.groups,
                    clone_depth=1,
                    origin=f"{proj.path}/lineage.dependencies",
                    url=project_url(remote, name, manifest.manifest_url),
                )
                manifest.projects[target] = new
                seen.add(target)
                nxt.append(new)
                added += 1
        frontier = sorted(nxt, key=lambda p: p.path)
    if depth >= max_depth:
        raise ManifestError(f"lineage.dependencies did not settle within {max_depth} levels")
    return manifest
