"""Print the project paths declared in one shard's local manifest.

Used by the workflow to ask repo for exactly one section's source:
    repo sync -c $(python3 tools/shard_paths.py local_manifests/shard-w0.xml)
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = ET.parse(argv[1]).getroot()
    paths = [p.get("path") for p in root.findall("project") if p.get("path")]
    if not paths:
        print(f"{argv[1]}: no projects", file=sys.stderr)
        return 1
    print("\n".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
