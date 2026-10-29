"""`probe.py applied <role>` — whether a setup role's files on its hosts match the tree.

Sessions confirmed an applied setup role by hand: `sha256sum` and `stat` on each installed
file, over ssh when the role lives on the other node, compared with the render by eye (#6326).
This renders the role's files from the working tree, reads each path on every host a play that
applies the role admits in one `host_reader` call per host, and prints one row per path.
`applied_hosts` says which task files each host runs: a role whose server and agent halves
are separate plays, as k3s's are, is walked half by half (#6475). The rows:

- `ok`: present with the declared mode, owner and group, and the content hash of the render;
- `ok-meta`: the same, except that the content was not compared, with the reason. A render
  holding a secret leaves a `STUB`, even through `| default("")`, because every key name in
  `secrets.yml` renders as `STUB` here. rootq refuses to hash a credential file, whose row
  says `content not compared: credential file`. rootq's `stat` still answers mode and owner
  for a credential file on its allowlist (#6369), so those are compared;
- `DRIFT`: missing, present where an absent arm removes it, or a declared field or the
  content differs;
- `UNCHECKED`: a credential file rootq refused even to stat, so nothing about it was
  verified (#6372), or a file whose content reads a name only the run sets, such as a
  `register`, so the render cannot predict it (#6378). Mode and owner are still compared, and
  a difference there is DRIFT;
- `unread`: the path could not be stat'ed as this user, and rootq did not answer for it,
  because the path is not on rootq's allowlist or the host has no rootq.

A path this user cannot stat or open is asked of `rootq` on its host, inside the same host
read: `rootq stat` for mode and owner, and `rootq matches PATH HEX` against the render's hash.
Both are tier 1 verbs, so no file content is printed, and `applied` calls no other (#6357).

`applied_tasks` says which tasks it reads and which it skips; the skipped ones are listed too.
A task file the walk cannot read prints as `UNCHECKED` with the importing file and the reason,
because nothing in it was read: its path does not resolve, or its `when:` or loop reads a
value only the run sets. An import whose `when:` is false for the host prints as skipped.
Exit 1 on any `DRIFT`, else 75 when a path was `unread`, a task file or a credential file was
`UNCHECKED` or no host had a path to compare, else 0. A 75 for an `UNCHECKED` row says on stderr that
coverage was partial.
The render is the working tree's, so commit or stash first to check `HEAD`.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from functools import cache
from pathlib import Path

from deploy_tools.setup_lib.setup_gates import HOSTS
from diagnostics.probe_lib import core
from diagnostics.probe_lib.applied_hosts import task_files
from diagnostics.probe_lib.applied_facts import STUB
from diagnostics.probe_lib.applied_tasks import Skip, Want, expected
from diagnostics.probe_lib.host_reader import dry_run_text, read_host
from lib import yaml_fast
from lib.ansible_inventory import inventory_hosts
from lib.exit_codes import PROBE_FAILED, PROBE_OK, PROBE_UNREADABLE
from lib.render_context import render_context
from lib.repo_paths import ANSIBLE
from validate.setup_templates import ANSIBLE_RUNTIME_CONTEXT

SETUP = ANSIBLE / "roles" / "setup"
# Read for its key names alone, which SOPS leaves in plaintext; no value is decrypted.
SECRETS = ANSIBLE / "vars" / "secrets.yml"

# rootq's refusals of a credential file, whose hash would be a guessing oracle for it. A
# refused hash reads ok-meta, `credential file`; a refused stat reads UNCHECKED.
CREDENTIAL_REFUSALS = ("is on the deny list", "matches a credential pattern")

APPLIED_HELP = (
    "whether a setup role's files on each host its playbook admits match the working "
    "tree's render: existence, declared mode, owner and group, and the content hash where "
    "the render holds no secret and the file is readable",
    "every compared path matches",
    "a path is missing, should be absent, or differs",
)


def secret_stubs(secrets: Path = SECRETS) -> dict[str, str]:
    """Every key name in `secrets`, each set to `STUB`.

    The static vars hold no secret, so without these a template's `{{ token | default("") }}`
    renders an empty value and its hash reads as a real expectation. The live file holds the
    token, so the row read DRIFT after every correct apply (#6475).
    """
    data = yaml_fast.safe_load(secrets.read_text()) if secrets.is_file() else {}
    return {str(k): STUB for k in data or {} if k != "sops"}


@cache
def inventory_magic() -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Ansible's `groups` and `hostvars` for the repo's inventory, as a play sees them.

    `hostvars[<host>]` holds the host's inventory vars: all.yml under its host_vars, with no
    role defaults, as Ansible keeps them. Without both, a template such as the agent's ssh
    config, which loops over `groups['all']` and reads each peer's flag, reads as run-time
    and is never compared (#6484).
    """
    hosts = inventory_hosts()
    groups: dict[str, list[str]] = {"all": [h.name for h in hosts], "ungrouped": []}
    for h in hosts:
        for group in h.groups or ("ungrouped",):
            groups.setdefault(group, []).append(h.name)
    hostvars = {
        h.name: {
            **render_context(None, host=h.name, overrides=secret_stubs()),
            **h.settings,
            "inventory_hostname": h.name,
        }
        for h in hosts
    }
    return groups, hostvars


def context(role_dir: Path, host: str) -> dict:
    ctx = render_context(
        role_dir,
        host=host,
        overrides={
            **secret_stubs(),
            **ANSIBLE_RUNTIME_CONTEXT,
            "inventory_hostname": host,
        },
    )
    # Set after the render, so a value already resolved is never templated a second time.
    ctx["groups"], ctx["hostvars"] = inventory_magic()
    return ctx


def credential_refusal(live: dict) -> str | None:
    """rootq's reason when it refused the path as a credential file, else None."""
    reason = live.get("refused") or ""
    return reason if any(r in reason for r in CREDENTIAL_REFUSALS) else None


def compare(want: Want, live: dict, user: str) -> tuple[str, str]:
    """(status, detail) for one path; `live` is `host_reader`'s stat row for it."""
    status = live.get("status")
    if status not in ("ok", "absent"):
        # A credential refusal of `stat` says nothing about the file: rootq refuses a DENY
        # path off its allowlist before it looks. Nothing was verified, so the row never
        # counts toward exit 0 (#6372).
        if (reason := credential_refusal(live)) is not None:
            return "UNCHECKED", f"credential file rootq would not stat: {reason}"
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
    if want.unchecked:
        return "UNCHECKED", f"{meta}, content not compared: {want.why_no_content}"
    why = want.why_no_content or f"content unreadable as {user}"
    if want.sha256 is not None:
        why = live.get("refused") or f"content unreadable as {user}"
    if live.get("credential") or credential_refusal(live):
        why = "credential file"
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


def run_applied(
    ns, read=read_host, setup: Path = SETUP, render_ctx=None, files_of=task_files
) -> int:
    """Check the role on each of its hosts, print the rows, and return the exit code.

    `read`, `setup`, `render_ctx` and `files_of` exist for a test: the host read, the
    directory holding the setup roles, the context builder (`context` by default) and the
    reader of each host's task files.
    """
    render_ctx = render_ctx or context
    role_dir = setup / ns.role
    if not (role_dir / "tasks" / "main.yml").is_file():
        raise core.UsageError(
            f"applied: {role_dir} is not a setup role with tasks/main.yml"
        )
    if ns.host and ns.host not in HOSTS:
        raise core.UsageError(f"applied: --host must be one of {', '.join(HOSTS)}")
    hosts = files_of(ns.role, role_dir, ns.host)
    if not hosts:
        print(
            f"applied: no host runs {ns.role}: its gate or end_role guard admits none"
        )
        return PROBE_UNREADABLE
    statuses: set[str] = set()
    compared = False
    for host, files in hosts.items():
        wants, skips = expected(
            role_dir, host, render_ctx(role_dir, host), tuple(files)
        )
        # A path two tasks write takes the hash of the one that has one.
        asks = {w.path: w.sha256 for w in sorted(wants, key=lambda w: bool(w.sha256))}
        request = {"stat": sorted(asks), "rootq": asks}
        if ns.dry_run:
            print(dry_run_text(request, host))
            continue
        # A host with no path to compare is not read, so an unreachable one costs nothing.
        if not wants:
            doc = {"user": "no one", "stat": {}}
        else:
            try:
                doc = read(request, host)
            except core.Unreadable as exc:
                # One host down must not hide the others' rows; its own section says so.
                print(f"{host}:\n  unread   every path: {exc.message}")
                statuses.add("unread")
                continue
        lines, seen = report_host(host, wants, skips, doc)
        # A host whose files write nothing is not a gap. A run where no host had a path is:
        # it verified nothing.
        compared |= bool(wants)
        statuses |= seen
        print("\n".join(lines))
    if ns.dry_run:
        return PROBE_OK
    if not compared:
        statuses.add("unread")
    if "DRIFT" in statuses:
        return PROBE_FAILED
    if "UNCHECKED" in statuses:
        print(
            f"applied: coverage partial: an UNCHECKED task file, credential file or "
            f"file whose content reads a run-time name of {ns.role} was not read",
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
