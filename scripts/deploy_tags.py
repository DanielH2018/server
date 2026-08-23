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

Used by scripts/deploy.sh. `list`'s flat sorted output is meant to be stable enough to back a
shell completion script one day (so the completion list and this validator couldn't disagree —
they'd be one function) — no such completion script exists yet, here or in the chezmoi dotfiles
repo that owns this machine's shell config, so today that's just the intent `list`'s shape was
chosen to keep open, not something wired up. `describe` is the separate human-facing view; `list`
stays flat and one-per-line for that possible future consumer (see test_deploy_tags.py).

Run: uv run pytest scripts/test_deploy_tags.py
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
from pathlib import Path

import yaml

from _render_guard import (
    ALL_VARS,
    HOST_VARS,
    REPO,
    containers_entries,
    host_files,
)

# deploy_logic.py lives under the gitops_deploy role's files/ because that role's own script
# (gitops_deploy.py) imports it as a same-directory sibling. scripts/ needs the same pure
# git-diff-to-service-set logic for `changed`, and copying it would drift the two the first time
# either changes — so this reaches across the role boundary instead, the same way
# gitops_deploy.py reaches into its own directory.
DEPLOY_LOGIC_DIR = REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files"

# Every container role block-tags its tasks with these (see the role task files and the
# `--skip-tags deploy` config-only workflow in CLAUDE.md). They are legitimate --tags
# values, so they must not be reported as typos.
BLOCK_TAGS = frozenset({"config", "deploy", "cron"})

# Ansible's own reserved tag. `--tags always` is degenerate but not a typo.
RESERVED_TAGS = frozenset({"always"})


def service_tags(host_vars: Path = HOST_VARS) -> set[str]:
    """Every tag that selects a service, across all hosts and both platforms."""
    tags: set[str] = set()
    for path in host_files(host_vars):
        for entry in containers_entries(path):
            # deploy.yml:116 — `container_item.tags | default([container_item.name])`.
            # Mirror that precedence exactly, or an entry that overrides its tags would
            # be validated against a name that no longer selects it.
            tags.update(entry.get("tags") or [entry["name"]])
    return tags


def service_records(host_vars: Path = HOST_VARS) -> list[tuple[str, str, str]]:
    """(host, platform, tag) for every containers_list entry, across all hosts.

    Same source and the same `tags | default([name])` precedence as service_tags(), just kept
    per-entry instead of flattened to a set — `describe` groups by host/platform, which needs
    the host and platform back.
    """
    records: list[tuple[str, str, str]] = []
    for path in host_files(host_vars):
        for entry in containers_entries(path):
            # No host_vars file sets `platform` on a docker entry today (daniel-pi's don't
            # carry the key at all) — k8s is the one that's always explicit, so docker is the
            # default rather than an unlabelled third state.
            platform = entry.get("platform", "docker")
            for tag in entry.get("tags") or [entry["name"]]:
                records.append((path.stem, platform, tag))
    return records


def known_tags(host_vars: Path = HOST_VARS) -> set[str]:
    return service_tags(host_vars) | set(BLOCK_TAGS) | set(RESERVED_TAGS)


def dry_run_unsupported(all_vars: Path = ALL_VARS) -> set[str]:
    loaded = yaml.safe_load(all_vars.read_text()) or {}
    return set(loaded.get("k8s_dry_run_unsupported") or [])


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


def _cmd_describe(_args: argparse.Namespace) -> int:
    """Human-facing view of `list`'s flat output: grouped by host/platform, dry-run-unsupported
    services flagged. Does not touch `list`'s own shape — that stays pinned flat and sorted."""
    unsupported = dry_run_unsupported()
    records = service_records()
    hosts = sorted({host for host, _platform, _tag in records})
    for host in hosts:
        by_platform: dict[str, list[str]] = {}
        for h, platform, tag in records:
            if h == host:
                by_platform.setdefault(platform, []).append(tag)
        for platform in sorted(by_platform):
            print(f"{host} ({platform}):")
            for tag in sorted(set(by_platform[platform])):
                flag = "  [dry-run: unsupported]" if tag in unsupported else ""
                print(f"  {tag}{flag}")
    print(f"block tags: {', '.join(sorted(BLOCK_TAGS))}")
    print(f"reserved: {', '.join(sorted(RESERVED_TAGS))}")
    return 0


def _load_deploy_logic():
    """Import deploy_logic.py from the gitops_deploy role — see DEPLOY_LOGIC_DIR above for why
    it lives outside scripts/. Done lazily, inside this function rather than at module import
    time, so `validate`/`list`/`describe` never pay for or depend on this cross-directory import
    succeeding — only `changed` needs it."""
    sys.path.insert(0, str(DEPLOY_LOGIC_DIR))
    from deploy_logic import (  # noqa: E402  (see docstring above)
        broad_remediation,
        services_from_changed_paths,
    )

    return services_from_changed_paths, broad_remediation


def _git_diff_paths(ref: str, cwd: Path = REPO) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{ref}...HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _cmd_changed(args: argparse.Namespace) -> int:
    """Print, on stdout, the comma-joined --tags value for every service changed vs `ref`
    (default origin/master) — nothing else goes to stdout, so scripts/deploy.sh can capture it
    directly. Everything explaining the derivation goes to stderr."""
    services_from_changed_paths, broad_remediation = _load_deploy_logic()
    try:
        paths = _git_diff_paths(args.ref)
    except subprocess.CalledProcessError as exc:
        print(
            f"deploy --changed: `git diff {args.ref}...HEAD` failed: "
            f"{exc.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    if not paths:
        print(f"deploy --changed: no files differ from {args.ref}.", file=sys.stderr)
        return 0

    cs = services_from_changed_paths(paths)

    if cs.broad:
        print(
            f"deploy --changed: refusing. {len(paths)} file(s) changed vs {args.ref} include "
            "a broad path (shared template, inventory, or setup-plane) --changed cannot scope "
            "to a service list — guessing which services that touches would be worse than "
            "asking.",
            file=sys.stderr,
        )
        print(
            f"  Run manually instead: {broad_remediation(cs.broad_deploy, cs.broad_setup)}",
            file=sys.stderr,
        )
        return 3

    if cs.tasks:
        print(
            "deploy --changed: structural change (tasks/defaults/vars/handlers) in "
            f"{sorted(cs.tasks)} — not auto-deployed and not captured by a tag; review and "
            "deploy by hand if it needs to go out.",
            file=sys.stderr,
        )
    if cs.meta:
        print(
            f"deploy --changed: meta/deps.yml changed in {sorted(cs.meta)} — the deploy-order "
            "graph may have shifted; review manually.",
            file=sys.stderr,
        )
    if cs.secrets:
        print(
            "deploy --changed: ansible/vars/secrets.yml changed — a new value only reaches a "
            "service on THAT service's own deploy, so confirm the consuming service's tag is "
            "in the list below.",
            file=sys.stderr,
        )

    tags = sorted(cs.k8s | cs.services)
    if not tags:
        print(
            f"deploy --changed: no deployable service changed vs {args.ref}.",
            file=sys.stderr,
        )
        return 0

    print(
        f"deploy --changed: {len(tags)} service(s) changed vs {args.ref}: {', '.join(tags)}",
        file=sys.stderr,
    )
    print(",".join(tags))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="exit 2 if any tag matches no service")
    v.add_argument("tags", nargs="*")
    v.set_defaults(func=_cmd_validate)

    lst = sub.add_parser("list", help="print every valid tag, one per line")
    lst.set_defaults(func=_cmd_list)

    desc = sub.add_parser(
        "describe", help="human-facing view of `list`, grouped by host/platform"
    )
    desc.set_defaults(func=_cmd_describe)

    ch = sub.add_parser(
        "changed",
        help="print --tags for services changed vs a ref (default origin/master); "
        "exit 3 refuses a broad change",
    )
    ch.add_argument("ref", nargs="?", default="origin/master")
    ch.set_defaults(func=_cmd_changed)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
