"""Step 3b: run the `activate:` action of each packaged file whose hash changed.

dpkg installs a file and nothing applies it. A `component.yml` file entry therefore declares an
`activate:` action (docs/target-architecture/host/h7-delete-ansible.md, *How moved config becomes
live*), and the renderer writes the entries into the manifest's `activations` map, path to action:

    none                         a file the program reads on each use, such as limits.d
    try-restart:<unit>           systemctl try-restart, after the unit's validation
    reload:<unit>                systemctl reload, after the unit's validation
    sysctl                       sysctl --system, after parsing the changed files
    modules-load                 restart systemd-modules-load.service
    reboot                       mark the host reboot-pending
    manual:<unit>                mark the unit pending and change nothing live

H4's `udev` action joins this list with its arm. A unit the manifest marks `manual` or
`windowed` is never restarted here, so an action on one marks it pending, as `manual:` does.

A validation failure leaves the live service untouched, writes /run/homelab/validate-failed, and
fails the run, which holds the host. The flag blocks the weekly reboot until a later run passes
every validation. Each entry advances in applied.json only when its action succeeded, so the
next run retries exactly the actions that failed.
"""

import hashlib
import re
from pathlib import Path

from reconcile_lib.common import ERR, ROOT, run, log, systemctl, under_root

RUN_DIR = ROOT / "run/homelab"
VALIDATE_FAILED = RUN_DIR / "validate-failed"
REBOOT_PENDING = RUN_DIR / "reboot-pending"

_UNIT = r"[A-Za-z0-9@_.:-]+\.(?:service|timer|socket|path)"
ACTION = re.compile(
    rf"none|sysctl|modules-load|reboot|(?:try-restart|reload|manual):{_UNIT}"
)
# The commands that check a service's configuration before reconcile reloads or restarts it.
COMMANDS = {
    "ssh.service": ("sshd", "-t"),
    "fail2ban.service": ("fail2ban-client", "-t"),
}
# The files reconcile parses itself, by the action kind or the unit, and the grammar of a line.
PARSERS = {"sysctl": "sysctl", "systemd-journald.service": "ini"}
LINE = {
    "sysctl": re.compile(r"-?[A-Za-z0-9_./-]+\s*=.*"),
    "ini": re.compile(r"\[[A-Za-z]+\]|[A-Za-z0-9]+\s*=.*"),
}
MODULES_UNIT = "systemd-modules-load.service"
# systemctl is-active prints these for a unit that is running or about to run.
RUNNING = frozenset({"active", "activating", "reloading"})


def valid_activations(value: object) -> bool:
    """True when `value` maps absolute packaged paths to actions this module knows."""
    return isinstance(value, dict) and all(
        isinstance(path, str)
        and path.startswith("/")
        and ".." not in path.split("/")
        and isinstance(action, str)
        and ACTION.fullmatch(action)
        for path, action in value.items()
    )


def file_hash(path: str) -> str | None:
    try:
        return hashlib.sha256(under_root(Path(path)).read_bytes()).hexdigest()
    except OSError:
        return None


def bad_line(path: str, grammar: str) -> str | None:
    """The first line of `path` that is neither blank, a comment nor in `grammar`."""
    text = under_root(Path(path)).read_text()
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if (
            stripped
            and stripped[0] not in "#;"
            and not LINE[grammar].fullmatch(stripped)
        ):
            return f"{path}:{number}: {stripped!r}"
    return None


def validate(kind: str, unit: str, paths: list[str]) -> str | None:
    """Why the changed `paths` must not go live, or None when they may."""
    command = COMMANDS.get(unit) if kind in ("reload", "try-restart") else None
    if command:
        result = run(*command)
        if result.returncode != 0:
            return f"`{' '.join(command)}` failed: {result.stderr.strip()}"
    grammar = PARSERS.get(kind) or PARSERS.get(unit)
    if grammar:
        for path in paths:
            found = bad_line(path, grammar)
            if found:
                return f"unparsable line {found}"
    return None


def invocation(unit: str) -> str:
    return systemctl("show", "-p", "InvocationID", "--value", unit).stdout.strip()


def perform(
    kind: str, unit: str, policies: dict, called: dict[str, bool], pending: dict
) -> bool:
    """Run one validated action. `called` maps each unit already called this run to its result."""
    if kind == "none":
        return True
    if kind == "reboot":
        REBOOT_PENDING.parent.mkdir(parents=True, exist_ok=True)
        # Never re-touch: the flag's mtime is when the host first owed a reboot since its last
        # boot, which homelab_host_reboot_pending_since_seconds exports (#6166).
        if not REBOOT_PENDING.exists():
            REBOOT_PENDING.touch()
        return True
    if kind == "sysctl":
        return run("sysctl", "--system").returncode == 0
    verb = kind
    if kind == "modules-load":
        unit, verb = MODULES_UNIT, "restart"
    elif kind == "manual" or policies.get(unit, {}).get("policy"):
        pending[unit] = invocation(unit)
        log(f"{unit} is pending: a change owed to it goes live at its next restart")
        return True
    if unit in called:
        return called[unit]  # at most one call per unit per run
    if verb == "reload" and systemctl("is-active", unit).stdout.strip() not in RUNNING:
        return True
    called[unit] = systemctl(verb, unit).returncode == 0
    if called[unit]:
        log(f"{verb} {unit}")
    else:
        log(f"{verb} {unit} failed", ERR)
    return called[unit]


def refresh_pending(pending: dict, activations: dict) -> dict:
    """The pending units still owed a restart: named by the manifest and not restarted since."""
    named = {action.partition(":")[2] for action in activations.values()}
    return {
        unit: seen
        for unit, seen in pending.items()
        if unit in named and invocation(unit) == seen
    }


def activate(
    activations: dict,
    prev_files: dict,
    policies: dict,
    called: dict[str, bool],
    pending: dict,
) -> tuple[bool, dict]:
    """Run the changed entries' actions. Return whether all succeeded, and the advanced hashes.

    `pending` is updated in place. The flag file clears first, so a failure a revert removed
    does not block the reboot.
    """
    VALIDATE_FAILED.unlink(missing_ok=True)
    ok, files = True, {}
    changed: dict[str, list[str]] = {}
    now = {path: file_hash(path) for path in activations}
    for path, action in sorted(activations.items()):
        if now[path] is None:
            log(f"{path} is missing; not activating it", ERR)
            ok = False
        elif now[path] != prev_files.get(path):
            changed.setdefault(action, []).append(path)
        else:
            files[path] = now[path]
    for action, paths in sorted(changed.items()):
        kind, _, unit = action.partition(":")
        problem = validate(kind, unit, paths)
        if problem:
            log(f"{action}: {problem}; leaving the running service as it is", ERR)
            VALIDATE_FAILED.parent.mkdir(parents=True, exist_ok=True)
            VALIDATE_FAILED.touch()
        succeeded = problem is None and perform(kind, unit, policies, called, pending)
        for path in paths:
            if succeeded:
                files[path] = now[path]
            elif path in prev_files:
                files[path] = prev_files[path]
        ok = ok and succeeded
    return ok, files
