"""Slice a build into sections, and map any path in the tree onto one.

A "section" is the unit that gets handed to one worker.  It is derived from
where the source lives, which is the only thing the manifest and the ninja
graph agree on: the manifest gives us `external/foo`, the ninja graph gives us
`out/.../obj/external/foo/...` edges fed by `external/foo/**/*.cc`.

The default policy follows how Android is actually layered, not how it is
alphabetised: the runtime (art/bionic/libcore) is one section because it is
mutually recursive, `prebuilts/` is copy-only and never compiled, and the
device + vendor trees are their own section because they are what the user
actually changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# First path component -> section.  Order matters only for readability.
POLICY: list[tuple[str, str]] = [
    ("prebuilts", "prebuilts"),
    ("device", "device"),
    ("vendor", "device"),
    ("kernel", "kernel"),
    ("trusty", "trusty"),
    ("external", "external"),
    ("frameworks", "frameworks"),
    ("packages", "packages"),
    ("system", "system"),
    ("hardware", "hardware"),
    ("build", "build"),
    ("art", "runtime"),
    ("bionic", "runtime"),
    ("libcore", "runtime"),
    ("dalvik", "runtime"),
    ("tools", "tooling"),
    ("bootable", "tooling"),
    ("test", "tooling"),
    ("cts", "tooling"),
    ("developers", "tooling"),
    ("development", "tooling"),
    ("pdk", "tooling"),
]

FALLBACK = "misc"


@dataclass
class Policy:
    rules: list[tuple[str, str]]

    @classmethod
    def default(cls) -> "Policy":
        return cls(list(POLICY))

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        """Rebuild from as_dict(), and refuse to silently become a no-op.

        A dict unpacked as a 2-tuple yields its keys, so a naive round trip
        turned every rule into prefix="prefix", section="section" and every
        project fell through to "misc". That has to be an error, not a fallback.
        """
        rules: list[tuple[str, str]] = []
        for r in data.get("rules", []):
            if not isinstance(r, dict) or "prefix" not in r or "section" not in r:
                raise ValueError(f"bad section rule {r!r}: need prefix + section")
            rules.append((str(r["prefix"]).strip("/"), str(r["section"])))
        return cls(rules)

    def section_of(self, path: str) -> str:
        p = path.lstrip("./")
        if p.startswith("out/") or p.startswith("out\\"):
            return "out"  # never a real section; callers propagate instead
        head = p.split("/", 1)[0]
        for prefix, section in self.rules:
            if head == prefix:
                return section
        return FALLBACK

    def as_dict(self) -> dict:
        return {"rules": [{"prefix": p, "section": s} for p, s in self.rules]}


def sections_for_projects(paths: list[str], policy: Policy) -> dict[str, list[str]]:
    """Group manifest project paths by section."""
    out: dict[str, list[str]] = {}
    for p in paths:
        out.setdefault(policy.section_of(p), []).append(p)
    for v in out.values():
        v.sort()
    return out
