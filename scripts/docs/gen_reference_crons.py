#!/usr/bin/env python3
"""Generate docs/reference/crons.md — every scheduled job the tree installs.

WHY THIS PAGE IS WORTH HAVING. Some of these jobs commit, push, deploy or delete without a
human in the loop. That set is the reason to read the page, and it is not obvious from any
single role: the tasks are spread across roles/setup/, roles/k8s/ and roles/containers/.

STATIC PARSING ONLY. Every `ansible.builtin.cron` task is read with yaml.safe_load. Jinja
in a schedule or a job line is printed as written, since it cannot be resolved without a
real deploy — an unresolved `{{ var }}` is the honest rendering, not a guess.

WHAT IT CANNOT DECIDE. Whether a job changes state is judged from its command by the
keyword list below, and a job whose command is a wrapper script is marked as needing the
script read. That is a heuristic and says so on the page; nothing in a cron task declares
its own blast radius.

Usage::

    uv run python scripts/docs/gen_reference_crons.py --out docs/reference/crons.md
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO, ROLES  # noqa: E402

# Commands that change durable state somewhere. Deliberately over-broad: a job wrongly
# flagged as state-changing costs a reader one moment, and one wrongly cleared could cost
# an incident.
_MUTATING = (
    "git commit",
    "git push",
    "deploy",
    "rotate",
    "rm ",
    "delete",
    "prune",
    "truncate",
    "restart",
    "apply",
    "backup",
    "snapshot",
    "reboot",
)

_HOST_RE = re.compile(r"inventory_hostname\s*==\s*'([^']+)'")


def _cron_tasks(path: Path) -> list[dict]:
    """Every ansible.builtin.cron task in one tasks file."""
    try:
        loaded = yaml.safe_load(path.read_text())
    except OSError, yaml.YAMLError:
        return []
    if not isinstance(loaded, list):
        return []
    return [t for t in loaded if isinstance(t, dict) and "ansible.builtin.cron" in t]


def _host_for(task: dict) -> str:
    """The host a `when:` restricts the task to, or 'all hosts in the play'."""
    when = task.get("when")
    text = " ".join(when) if isinstance(when, list) else str(when or "")
    match = _HOST_RE.search(text)
    if match:
        return match.group(1)
    if "has_gitops" in text:
        return "the gitops host"
    return "every host in the play" if not text else f"conditional ({text[:60]})"


def _schedule(spec: dict) -> str:
    if spec.get("special_time"):
        return f"@{spec['special_time']}"
    fields = [
        str(spec.get(k, "*")) for k in ("minute", "hour", "day", "month", "weekday")
    ]
    return " ".join(fields)


def _changes_state(job: str) -> str:
    lowered = job.lower()
    hits = [word.strip() for word in _MUTATING if word in lowered]
    if hits:
        return f"yes ({', '.join(sorted(set(hits)))})"
    if ".sh" in lowered:
        return "read the script"
    return "no (read-only by its command)"


def _rel(path: Path, roles: Path) -> str:
    """Repo-relative path where possible, else relative to the roles root.

    Tests pass a tmp_path as `roles`, which is not under REPO — so anchoring only on
    REPO raises rather than degrading.
    """
    for base in (REPO, roles.parent, roles):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def build_rows(roles: Path = ROLES) -> list[dict[str, str]]:
    rows = []
    for path in sorted(roles.rglob("tasks/*.yml")):
        if "/archive/" in path.as_posix():
            continue
        for task in _cron_tasks(path):
            spec = task["ansible.builtin.cron"] or {}
            if str(spec.get("state", "present")) == "absent":
                continue
            job = str(spec.get("job", ""))
            rows.append(
                {
                    "name": str(spec.get("name", task.get("name", "unnamed"))),
                    "schedule": _schedule(spec),
                    "host": _host_for(task),
                    "user": str(spec.get("user", "root")),
                    "changes_state": _changes_state(job),
                    "source": _rel(path, roles),
                }
            )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    from lib.docs_provenance import generated_banner

    parts = [generated_banner("scripts/docs/gen_reference_crons.py")]
    parts.append("# Scheduled jobs\n")
    parts.append(f"{len(rows)} cron entrie(s) installed across the roles.\n")
    parts.append(
        '!!! warning "The state column is a heuristic"\n'
        "    It is judged from the command text, and nothing in a cron task declares its "
        'own blast radius. A job that runs a wrapper script reads as "read the script" '
        "rather than being guessed at. Treat it as a pointer, not an authority.\n"
    )

    parts.append("| Job | Schedule | Host | User | Changes state | Defined in |")
    parts.append("|---|---|---|---|---|---|")
    for row in sorted(rows, key=lambda r: r["name"]):
        parts.append(
            f"| {row['name']} | `{row['schedule']}` | {row['host']} | "
            f"`{row['user']}` | {row['changes_state']} | `{row['source']}` |"
        )

    parts.append(
        "\n## Schedule format\n\n"
        "Five fields: minute, hour, day-of-month, month, day-of-week. A value still "
        "showing `{{ ... }}` is an Ansible variable that only resolves at deploy time — "
        "these pages are rendered by static parsing and never run Ansible, so the "
        "template is the honest rendering.\n"
    )
    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--roles", type=Path, default=ROLES)
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows(args.roles)
    return finish_generator(
        "gen_reference_crons", args.out, rows, render_markdown, "cron"
    )


if __name__ == "__main__":
    raise SystemExit(main())
