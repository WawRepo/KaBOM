#!/usr/bin/env python3
"""Assert that piped-in rendered Helm output is usable Kubernetes YAML.

`helm lint` and `helm template` both pass on a manifest whose apiVersion
has been swallowed by a stray `-}}` chomp: the chomp eats the newline after
a template action and glues `apiVersion:` onto the end of the previous
line, so YAML parses it as part of that line and the document simply has no
apiVersion. Helm never notices, because it only ever produced text. The
failure lands on whoever installs the chart:

    ComparisonError: failed to discover server resources for group
    version : groupVersion shouldn't be empty

That shipped as chart 0.1.3 and could not be installed by ArgoCD at all.
Parsing the render is the only thing that catches it.

Usage: helm template ... | python3 scripts/check_manifests.py
"""

from __future__ import annotations

import sys

import yaml


def main() -> int:
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]

    if not docs:
        print("    rendered no documents at all", file=sys.stderr)
        return 1

    bad = [d for d in docs if not d.get("apiVersion") or not d.get("kind")]
    for d in bad:
        kind = d.get("kind")
        api = d.get("apiVersion")
        print(f"    UNUSABLE: kind={kind!r} apiVersion={api!r}", file=sys.stderr)
    if bad:
        print(
            f"    {len(bad)} of {len(docs)} documents lack apiVersion or kind",
            file=sys.stderr,
        )
        return 1

    for d in docs:
        print(f"    {d['kind']:<24} {d['apiVersion']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
