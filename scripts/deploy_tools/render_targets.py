#!/usr/bin/env python3
"""The k8s services a render-record run renders on one host, derived from the tree.

`render_records` (roles/setup/render_records) runs `deploy.sh --dry-run -e
manifests_render_record=true` hourly, and this module is its `--tags` list (#2587). A service
qualifies when three things hold:

  * the host's `containers_list` declares it with `platform: k8s`, because a deploy reaches
    only the host it runs on, and a record's `host` must equal the release record's;
  * its role includes `k8s/manifests`, because render_record.yml runs inside that role and a
    role that applies its objects some other way writes no record;
  * it is not in `k8s_dry_run_unsupported`, which deploy.yml refuses under a dry run.

Derived rather than listed. A list goes stale the day a service is added, and a service missing
from it reads to the staleness reader as one with nothing to compare.

The producer runs this module from the checkout it is about to render, so the list belongs to
the commit whose records it writes.

Run: uv run python scripts/deploy_tools/render_targets.py daniel-box
     uv run pytest scripts/deploy_tools/tests/test_render_targets.py
"""

import argparse
import re
import sys
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from lib import yaml_fast
from lib.render_guard import containers_entries, entry_platform, entry_tags
from lib.repo_paths import ALL_VARS, HOST_VARS, K8S_ROLES

# The role name of an `include_role`/`import_role`, as every includer spells it:
# `name: k8s/manifests` on its own line. A task NAME is prose and never exactly this string.
_INCLUDES_MANIFESTS = re.compile(r"^\s*name:\s*[\"']?k8s/manifests[\"']?\s*$", re.M)


def includes_manifests(role: Path) -> bool:
    """Whether any of `role`'s task files includes the shared `k8s/manifests` role."""
    return any(
        _INCLUDES_MANIFESTS.search(path.read_text())
        for path in sorted((role / "tasks").glob("*.yml"))
    )


def render_targets(
    host: str,
    host_vars: Path = HOST_VARS,
    roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> list[str]:
    """The sorted service names a render-record run on `host` renders, each its own tag.

    Empty for a host whose `host_vars` file is missing: that host declares no services.
    """
    path = host_vars / f"{host}.yml"
    if not path.exists():
        return []
    loaded = yaml_fast.safe_load(all_vars.read_text()) or {}
    unsupported = set(loaded.get("k8s_dry_run_unsupported") or [])
    # The entry NAME, not every tag it carries: the name is the role, the role's
    # `manifests_service`, and so the record's filename, which is what the producer reads
    # back. It also selects the entry, since `entry_tags` defaults to it.
    names: set[str] = set()
    for entry in containers_entries(path):
        name = entry["name"]
        if entry_platform(entry) != "k8s" or name in unsupported:
            continue
        if name in entry_tags(entry) and includes_manifests(roles / name):
            names.add(name)
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="the inventory hostname whose services to list")
    args = parser.parse_args(argv)
    tags = render_targets(args.host)
    if not tags:
        print(
            f"render_targets: no renderable k8s service on {args.host}", file=sys.stderr
        )
        return 1
    print(",".join(tags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
