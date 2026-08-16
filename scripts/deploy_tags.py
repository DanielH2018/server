#!/usr/bin/env python3
"""Validate the --tags a deploy was given, before Ansible silently accepts them.

THE PROBLEM. Ansible does not error on a tag that matches nothing. Every static task
in ansible/deploy.yml is `tags: always`, and the per-service tags are attached at
runtime by include_role from `container_item.name` (deploy.yml:116,
tasks/k8s_batch.yml:19). So `--tags jellifin` runs the secrets preamble, the namespace
apply, the rollout drain and the stabilisation gate, prints a green PLAY RECAP, and
deploys nothing. Confirmed 2026-08-16 with `--tags jellifin --list-tasks`: the play is
valid and no service matches.

That fails in the worst direction — it reports success while shipping nothing, so the
operator believes the change is live. With ~50 service names, several of them near
misses of one another (sonarr/radarr, n8n/n8n-images, wg-easy on two hosts), a typo is
not hypothetical.

WHAT COUNTS AS VALID. The union of:
  - every `containers_list` entry name across inventory/host_vars/*.yml, both platforms
    (entries carry no `tags:` override today, so the tag IS the name; if one ever grows
    a `tags:` key it is read here too rather than being silently dropped)
  - the block tags every container role is tagged with: config, deploy, cron
  - `always`, which Ansible treats specially

Used by scripts/deploy.sh. `list` also backs shell completion, so the completion list
and the validator can never disagree — they are one function.

Run: uv run pytest scripts/test_deploy_tags.py
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
HOST_VARS = REPO / "ansible" / "inventory" / "host_vars"

# Every container role block-tags its tasks with these (see the role task files and the
# `--skip-tags deploy` config-only workflow in CLAUDE.md). They are legitimate --tags
# values, so they must not be reported as typos.
BLOCK_TAGS = frozenset({"config", "deploy", "cron"})

# Ansible's own reserved tag. `--tags always` is degenerate but not a typo.
RESERVED_TAGS = frozenset({"always"})


def service_tags(host_vars: Path = HOST_VARS) -> set[str]:
    """Every tag that selects a service, across all hosts and both platforms."""
    tags: set[str] = set()
    # `_example.yml` is a template, not a host — same exclusion the role-existence test
    # makes (ansible/tests/test_containers_list_roles_exist.py).
    for path in sorted(
        p for p in host_vars.glob("*.yml") if not p.name.startswith("_")
    ):
        loaded = yaml.safe_load(path.read_text()) or {}
        for entry in loaded.get("containers_list") or []:
            name = entry.get("name")
            if not name:
                continue
            # deploy.yml:116 — `container_item.tags | default([container_item.name])`.
            # Mirror that precedence exactly, or an entry that overrides its tags would
            # be validated against a name that no longer selects it.
            tags.update(entry.get("tags") or [name])
    return tags


def known_tags(host_vars: Path = HOST_VARS) -> set[str]:
    return service_tags(host_vars) | set(BLOCK_TAGS) | set(RESERVED_TAGS)


def unknown_tags(tags: list[str], host_vars: Path = HOST_VARS) -> list[str]:
    known = known_tags(host_vars)
    # Order-preserving and de-duplicated, so the message reads in the order typed.
    seen: set[str] = set()
    out = []
    for tag in tags:
        if tag and tag not in known and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def suggest(tag: str, host_vars: Path = HOST_VARS) -> list[str]:
    """Nearest known tags, for a 'did you mean' line. Empty when nothing is close."""
    return difflib.get_close_matches(
        tag, sorted(known_tags(host_vars)), n=3, cutoff=0.6
    )


def _cmd_validate(args: argparse.Namespace) -> int:
    bad = unknown_tags(args.tags)
    if not bad:
        return 0
    for tag in bad:
        print(f"deploy: no service or block tag named '{tag}'.", file=sys.stderr)
        matches = suggest(tag)
        if matches:
            print(f"  Did you mean: {', '.join(matches)}?", file=sys.stderr)
    print(
        "  Nothing was deployed. Ansible would have exited 0 having done nothing.\n"
        "  `scripts/deploy.sh --list-services` shows every valid tag; "
        "--skip-tag-check bypasses this.",
        file=sys.stderr,
    )
    return 2


def _cmd_list(_args: argparse.Namespace) -> int:
    for tag in sorted(known_tags()):
        print(tag)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="exit 2 if any tag matches no service")
    v.add_argument("tags", nargs="*")
    v.set_defaults(func=_cmd_validate)

    lst = sub.add_parser("list", help="print every valid tag, one per line")
    lst.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
