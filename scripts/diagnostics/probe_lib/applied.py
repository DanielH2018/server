"""`probe.py applied <role>` — whether a setup role's files on its hosts match the tree.

Sessions confirmed an applied setup role by hand: `sha256sum` and `stat` on each installed
file, over ssh when the role lives on the other node, compared with the render by eye (#6326).
This renders the role's files from the working tree, reads each path on every host the role's
playbook entry admits in one `host_reader` call per host, and prints one row per path:

- `ok`: present with the declared mode, owner and group, and the content hash of the render;
- `ok-meta`: the same, except that the content was not compared, with the reason. A render
  holding a secret leaves a `STUB`, and rootq refuses to hash a credential file. A credential
  path rootq refuses even to stat, such as a `*.env` file, reads `ok-meta` with no metadata;
- `DRIFT`: missing, present where an absent arm removes it, or a declared field or the
  content differs;
- `unread`: the path could not be stat'ed as this user, and rootq did not answer for it,
  because the path is not on rootq's allowlist or the host has no rootq.

A path this user cannot stat or open is asked of `rootq` on its host, inside the same host
read: `rootq stat` for mode and owner, and `rootq matches PATH HEX` against the render's hash.
Both are tier 1 verbs, so no file content is printed, and `applied` calls no other (#6357).

`applied_tasks` says which tasks it reads and which it skips; the skipped ones are listed too.
A task file the walk cannot read prints as `UNCHECKED` with the importing file and the reason,
because nothing in it was read: its path does not resolve, or its `when:` or loop reads a
value only the run sets. An import whose `when:` is false for the host prints as skipped.
Exit 1 on any `DRIFT`, else 75 when a path was `unread`, a task file was `UNCHECKED` or nothing
was compared, else 0. A 75 for an `UNCHECKED` task file says on stderr that coverage was partial.
The render is the working tree's, so commit or stash first to check `HEAD`.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from pathlib import Path

from deploy_tools.setup_lib.setup_gates import (
    HOSTS,
    GateUnreadable,
    eval_when_strict,
    role_end_hosts,
)
from diagnostics.probe_lib import core
from diagnostics.probe_lib.applied_tasks import Skip, Want, expected
from diagnostics.probe_lib.host_reader import dry_run_text, read_host
from lib import yaml_fast
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE
from lib.render_context import render_context
from lib.repo_paths import ANSIBLE
from validate.setup_templates import ANSIBLE_RUNTIME_CONTEXT

SETUP = ANSIBLE / "roles" / "setup"
# The playbooks that apply a setup role, in `setup_routing.PLAYBOOKS`' precedence order.
PLAYBOOKS = ("initial_setup.yml", "k3s-bringup.yml", "bootstrap.yml")

# rootq's refusals of a credential file, whose hash would be a guessing oracle for it. Such a
# path reads ok-meta with the reason, because no read could compare its content.
CREDENTIAL_REFUSALS = ("is on the deny list", "matches a credential pattern")

APPLIED_HELP = (
    "whether a setup role's files on each host its playbook admits match the working "
    "tree's render: existence, declared mode, owner and group, and the content hash where "
    "the render holds no secret and the file is readable",
    "every compared path matches",
    "a path is missing, should be absent, or differs",
)


def entry_when(role: str, ansible: Path = ANSIBLE):
    """The `when:` of the first playbook entry that applies `role`.

    Raises:
        core.UsageError: no playbook lists the role.
    """
    for name in PLAYBOOKS:
        path = ansible / name
        for play in (
            yaml_fast.safe_load(path.read_text()) if path.is_file() else []
        ) or []:
            for entry in (play or {}).get("roles") or []:
                if entry == role:
                    return None
                if isinstance(entry, dict) and entry.get("role") == role:
                    return entry.get("when")
    raise core.UsageError(
        f"applied: no playbook in {', '.join(PLAYBOOKS)} lists {role}"
    )


def hosts_for(role: str, role_dir: Path) -> list[str]:
    """The hosts the role's playbook entry admits and its leading `end_role` guard keeps.

    Raises:
        core.UsageError: the entry's `when:` cannot be read for some host, so `--host` must
            name the one to check.
    """
    when = entry_when(role)
    admitted = []
    for host in HOSTS:
        try:
            if when is None or eval_when_strict(when, host):
                admitted.append(host)
        except GateUnreadable as exc:
            raise core.UsageError(
                f"applied: {role}'s gate: {exc}; pass --host"
            ) from exc
    ended = role_end_hosts(role_dir, admitted)
    return [h for h in admitted if h not in ended]


def context(role_dir: Path, host: str) -> dict:
    return render_context(
        role_dir,
        host=host,
        overrides={**ANSIBLE_RUNTIME_CONTEXT, "inventory_hostname": host},
    )


def credential_refusal(live: dict) -> str | None:
    """rootq's reason when it refused the path as a credential file, else None."""
    reason = live.get("refused") or ""
    return reason if any(r in reason for r in CREDENTIAL_REFUSALS) else None


def compare(want: Want, live: dict, user: str) -> tuple[str, str]:
    """(status, detail) for one path; `live` is `host_reader`'s stat row for it."""
    status = live.get("status")
    if status not in ("ok", "absent"):
        if (reason := credential_refusal(live)) is not None:
            return "ok-meta", f"not stat'ed: rootq refused: {reason}"
        return "unread", f"cannot stat as {user}: {live.get('error', status)}"
    if not want.present:
        return (
            ("ok", "absent")
            if status == "absent"
            else ("DRIFT", "present; an absent arm removes it")
        )
    if status == "absent":
        return "DRIFT", "missing"
    diffs = [
        f"{name} {live.get(name)} (want {value})"
        for name, value in (
            ("mode", want.mode),
            ("owner", want.owner),
            ("group", want.group),
        )
        if value is not None and live.get(name) != value
    ]
    meta = f"{live.get('mode')} {live.get('owner')}:{live.get('group')}"
    if live.get("via") == "rootq":
        meta += " (rootq)"
    # `match` is rootq's verdict on the render's hash; a readable file carries its own hash.
    same = live.get("match")
    if want.sha256 is not None and live.get("sha256") is not None:
        same = live["sha256"] == want.sha256
    if want.sha256 is not None and same is not None:
        if not same:
            diffs.append("content differs from the render")
        elif not diffs:
            return "ok", f"{meta}, content matches"
    if diffs:
        return "DRIFT", "; ".join(diffs)
    why = want.why_no_content or f"content unreadable as {user}"
    if want.sha256 is not None:
        why = live.get("refused") or f"content unreadable as {user}"
    return "ok-meta", f"{meta}, content not compared: {why}"


def report_host(
    host: str, wants: list[Want], skips: list[Skip], doc: dict
) -> tuple[list[str], set[str]]:
    """The host's lines and the set of statuses its rows took."""
    user = doc.get("user", "?")
    lines = [f"{host} (read as {user}):"]
    seen = set()
    for want in wants:
        status, detail = compare(
            want, (doc.get("stat") or {}).get(want.path) or {}, user
        )
        seen.add(status)
        lines.append(f"  {status:<8} {want.path}  {detail}")
    for skip in skips:
        word = "UNCHECKED" if skip.unchecked else "skipped "
        lines.append(f"  {word} {skip.task}: {skip.reason}")
    if any(s.unchecked for s in skips):
        seen.add("UNCHECKED")
    if not wants:
        lines.append("  no path this probe compares")
    return lines, seen


def run_applied(ns, read=read_host, setup: Path = SETUP, render_ctx=None) -> int:
    """Check the role on each of its hosts, print the rows, and return the exit code.

    `read`, `setup` and `render_ctx` exist for a test: the host read, the directory holding
    the setup roles, and the context builder (`context` by default).
    """
    render_ctx = render_ctx or context
    role_dir = setup / ns.role
    if not (role_dir / "tasks" / "main.yml").is_file():
        raise core.UsageError(
            f"applied: {role_dir} is not a setup role with tasks/main.yml"
        )
    hosts = [ns.host] if ns.host else hosts_for(ns.role, role_dir)
    if ns.host and ns.host not in HOSTS:
        raise core.UsageError(f"applied: --host must be one of {', '.join(HOSTS)}")
    if not hosts:
        print(
            f"applied: no host runs {ns.role}: its gate or end_role guard admits none"
        )
        return PROBE_UNREADABLE
    statuses: set[str] = set()
    for host in hosts:
        wants, skips = expected(role_dir, host, render_ctx(role_dir, host))
        # A path two tasks write takes the hash of the one that has one.
        asks = {w.path: w.sha256 for w in sorted(wants, key=lambda w: bool(w.sha256))}
        request = {"stat": sorted(asks), "rootq": asks}
        if ns.dry_run:
            print(dry_run_text(request, host))
            continue
        try:
            doc = read(request, host)
        except core.Unreadable as exc:
            # One host down must not hide the others' rows; its own section says so.
            print(f"{host}:\n  unread   every path: {exc.message}")
            statuses.add("unread")
            continue
        lines, seen = report_host(host, wants, skips, doc)
        statuses |= seen if wants else seen | {"unread"}
        print("\n".join(lines))
    if ns.dry_run:
        return PROBE_OK
    if "DRIFT" in statuses:
        return PROBE_FAILED
    if "UNCHECKED" in statuses:
        print(
            f"applied: coverage partial: an UNCHECKED task file of {ns.role} was not read",
            file=_sys.stderr,
        )
        return PROBE_UNREADABLE
    return PROBE_UNREADABLE if "unread" in statuses else PROBE_OK


def add_parser(sub, verdict_help) -> None:
    """Add `applied` to probe.py's subparsers; it lives here while cli_parser.py sits at its cap."""
    ap = sub.add_parser("applied", **verdict_help(*APPLIED_HELP))
    ap.add_argument(
        "role", help="the setup role, as it is named under ansible/roles/setup/"
    )
    ap.add_argument(
        "--host", help="check this host only (default: every host the role runs on)"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the reads without making them"
    )
