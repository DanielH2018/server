#!/usr/bin/env python3
"""Clear the shared Ansible fact cache when it pins another worktree's interpreter.

THE PROBLEM. `ansible.cfg` sets `fact_caching = jsonfile`, `fact_caching_connection =
~/.cache/ansible/facts`, `fact_caching_timeout = 7200`. The cache is keyed by HOST
(`s1_daniel-box`), not by checkout, so every worktree on this machine shares one file per
host. What it stores includes `discovered_interpreter_python`, which under `uv run` is the
*calling* worktree's `.venv`. Whichever session gathers facts first therefore pins its own
interpreter for every other session, for the full 7200s.

Delete that worktree and every subsequent deploy — from any worktree — fails at Gathering
Facts with a message that names a module rather than the cache:

    The module interpreter '/home/ubuntu/server/.claude/worktrees/<gone>/.venv/bin/python3.14'
    was not found.
    "msg": "The following modules failed to execute: ansible.legacy.setup."

`PLAY RECAP` reads `ok=0 changed=0 failed=1` — nothing was built or deployed. Observed
2026-08-27 after the `texbrain-mjs-mime` worktree went away mid-session, and again within
minutes when a second session's `./scripts/deploy.sh --dry-run` re-pinned a third worktree.

WHY IT NEEDS A GUARD RATHER THAN A HABIT. Three things make it hard to catch by hand. The
two-hour TTL means it does not self-heal inside a session. The error points at Ansible, not
at a cache. And the ~9 minutes spent queuing on /var/lock/server-git-tree.lock happens
BEFORE the failure, so a deploy looks like it is building for ten minutes and then dies
having done nothing.

WHY A FOREIGN-BUT-LIVE PATH IS ALSO STALE. A cached interpreter under another worktree that
still exists resolves fine today and breaks the moment that worktree is pruned — the
poisoning is invisible while the pinning worktree lives. Waiting for the path to disappear
means discovering it during a deploy, which is the failure this guard exists to prevent. So
any interpreter under a `.claude/worktrees/<other>` is stale here, whether or not it
currently resolves. An interpreter in the primary checkout, or in OUR worktree, is fine.

WHY IT CLEARS RATHER THAN REFUSES. This is a cache; discarding it costs one re-gather. A
refusal would only tell the operator to run the `rm` themselves. `deploy.sh` therefore calls
this with --clear in its preflight. Run without --clear it reports and exits 1, which is what
the tests and an interactive check use.

NOT THE AUTOMATED PIPELINE. gitops_deploy.py invokes ansible-playbook from the primary
checkout, whose `.venv` is never pruned, so it cannot pin a doomed path.

Usage:
    uv run python scripts/deploy_tools/fact_cache_guard.py            # report, exit 1 if stale
    uv run python scripts/deploy_tools/fact_cache_guard.py --clear    # delete stale entries
    uv run python scripts/deploy_tools/fact_cache_guard.py --cache-dir DIR --repo-root DIR
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO

# The marker that makes a path worktree-local. Claude Code puts every session worktree under
# <repo>/.claude/worktrees/<name>, so the segment after it is the worktree's identity.
WORKTREE_SEGMENT = (".claude", "worktrees")

# Facts that name an interpreter. `discovered_interpreter_python` is the one Ansible reuses
# for module execution and so the one that fails the play; `ansible_python.executable` is
# recorded alongside it and is checked too so a half-written cache still trips the guard.
INTERPRETER_FACTS = ("discovered_interpreter_python",)
NESTED_INTERPRETER_FACTS = (("ansible_python", "executable"),)


def cache_dir_from_cfg(repo_root: Path) -> Path:
    """Read fact_caching_connection out of the repo's ansible.cfg.

    Falls back to Ansible's own default location when the file or key is absent, so a
    checkout without an ansible.cfg still gets a sane answer rather than an exception.
    """
    cfg_path = repo_root / "ansible.cfg"
    if cfg_path.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(cfg_path)
            raw = parser.get("defaults", "fact_caching_connection", fallback="")
        except configparser.Error:
            raw = ""
        if raw:
            return Path(os.path.expanduser(raw.strip()))
    return Path.home() / ".cache" / "ansible" / "facts"


def _payload(raw: str) -> dict:
    """Return the fact dict, unwrapping the `__payload__` envelope when present.

    Ansible's jsonfile cache writes `{"__payload__": "<json string>"}` on this version and a
    plain fact dict on others. Both shapes are read so the guard does not quietly stop
    matching after an ansible-core bump — a check that silently sees nothing is worse than
    one that errors.
    """
    doc = json.loads(raw)
    inner = doc.get("__payload__") if isinstance(doc, dict) else None
    if isinstance(inner, str):
        doc = json.loads(inner)
    elif isinstance(inner, dict):
        doc = inner
    return doc if isinstance(doc, dict) else {}


def interpreters(facts: dict) -> list[str]:
    """Every interpreter path this cache entry pins."""
    found = []
    for key in INTERPRETER_FACTS:
        value = facts.get(key)
        if isinstance(value, str) and value:
            found.append(value)
    for outer, inner in NESTED_INTERPRETER_FACTS:
        block = facts.get(outer)
        if isinstance(block, dict):
            value = block.get(inner)
            if isinstance(value, str) and value:
                found.append(value)
    return found


def worktree_name(path: str) -> str | None:
    """The worktree a path belongs to, or None when it is not under a worktree.

    Matches on the `.claude/worktrees` segment pair rather than a prefix string, so it is
    unaffected by where the repo itself lives.
    """
    parts = Path(path).parts
    for i in range(len(parts) - len(WORKTREE_SEGMENT)):
        if parts[i : i + len(WORKTREE_SEGMENT)] == WORKTREE_SEGMENT:
            tail = parts[i + len(WORKTREE_SEGMENT) :]
            return tail[0] if tail else None
    return None


def staleness(path: str, our_worktree: str | None) -> str | None:
    """Why `path` is unusable for us, or None when it is fine.

    Two independent reasons, reported separately because they read very differently to an
    operator: a path that is already gone is today's outage, and a foreign path that still
    resolves is tomorrow's.
    """
    if not Path(path).exists():
        return "names an interpreter that no longer exists"
    owner = worktree_name(path)
    if owner is not None and owner != our_worktree:
        return f"pins worktree '{owner}', which is not ours"
    return None


def scan(cache_dir: Path, our_worktree: str | None) -> list[tuple[Path, str, str]]:
    """Every (cache file, interpreter path, reason) triple that makes the cache unusable."""
    if not cache_dir.is_dir():
        return []
    stale = []
    for entry in sorted(cache_dir.iterdir()):
        if not entry.is_file():
            continue
        try:
            facts = _payload(entry.read_text())
        except OSError, json.JSONDecodeError:
            # An unreadable or malformed cache entry cannot be trusted to name a live
            # interpreter, and discarding it costs one re-gather. Fail closed.
            stale.append((entry, "", "is unreadable or malformed"))
            continue
        for path in interpreters(facts):
            reason = staleness(path, our_worktree)
            if reason:
                stale.append((entry, path, reason))
    return stale


def our_worktree_name(repo_root: Path) -> str | None:
    """The worktree this run is executing from, or None in the primary checkout."""
    return worktree_name(str(repo_root.resolve()))


def main() -> int:
    """Scan the fact cache for entries pinning a gone or foreign worktree, and report them.

    Exits 0 when nothing is stale, 1 when stale entries were found and not cleared (or a
    clear failed), and 0 after successfully clearing them with `--clear`.
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--clear",
        action="store_true",
        help="delete the stale cache entries instead of only reporting them",
    )
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="override the fact cache directory (default: read from ansible.cfg)",
    )
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=REPO,
        help="the checkout this deploy renders from (default: this script's repo)",
    )
    args = ap.parse_args()

    repo_root = args.repo_root.resolve()
    cache_dir = args.cache_dir or cache_dir_from_cfg(repo_root)
    ours = our_worktree_name(repo_root)

    stale = scan(cache_dir, ours)
    if not stale:
        return 0

    victims = sorted({entry for entry, _, _ in stale})
    for entry, path, reason in stale:
        detail = f" ({path})" if path else ""
        print(f"fact-cache: {entry.name} {reason}{detail}", file=sys.stderr)

    if not args.clear:
        print(
            "fact-cache: re-run with --clear, or `rm "
            + " ".join(str(v) for v in victims)
            + "`",
            file=sys.stderr,
        )
        return 1

    for entry in victims:
        try:
            entry.unlink()
        except OSError as exc:
            print(f"fact-cache: could not remove {entry}: {exc}", file=sys.stderr)
            return 1
    print(
        f"fact-cache: cleared {len(victims)} stale entr"
        f"{'y' if len(victims) == 1 else 'ies'}; facts will be re-gathered",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
