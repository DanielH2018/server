"""Step 5's udev arm (H4.3b1): reload and trigger `udev` when a shipped rule is new or has changed.

The design is docs/target-architecture/host/h4-secrets-unit.md, *NUT on daniel-server*, udev.
A package ships a rule as /usr/lib/udev/rules.d/NN-homelab-*.rules. The manifest names none: the
arm hashes the files on disk and compares them with `udev` in applied.json, a map of rule path to
sha256.

A rule that is new, or whose hash differs, runs `udevadm control --reload-rules` and then
`udevadm trigger --subsystem-match=usb`. Nothing else calls udevadm: not a host with no rule, not
an unchanged rule, and not a rule the package dropped. A drop only leaves the record, because a
trigger would apply the host's remaining rules to a device that kept the dropped rule's mode on
purpose until the next event. After a failed call, a changed rule keeps its old hash, so the next
run retries it.
"""

import hashlib
from pathlib import Path

from reconcile_lib.common import ERR, run, say, under_root

RULES = Path("/usr/lib/udev/rules.d")
GLOB = "[0-9][0-9]-homelab-*.rules"
CALLS = (
    ("udevadm", "control", "--reload-rules"),
    ("udevadm", "trigger", "--subsystem-match=usb"),
)


def reconcile(prev: object) -> tuple[bool, dict[str, str]]:
    """Run the arm against `prev`, the recorded hashes. Return its success and the hashes to record."""
    known = prev if isinstance(prev, dict) else {}
    try:
        shipped = {
            f"{RULES}/{rule.name}": hashlib.sha256(rule.read_bytes()).hexdigest()
            for rule in sorted(under_root(RULES).glob(GLOB))
            if rule.is_file()
        }
    except OSError as exc:
        say(f"cannot read the udev rules: {exc}", ERR)
        return False, {k: v for k, v in known.items() if isinstance(v, str)}
    if all(known.get(path) == digest for path, digest in shipped.items()):
        return True, shipped
    for argv in CALLS:
        result = run(*argv)
        if result.returncode != 0:
            say(f"{' '.join(argv)} failed: {result.stderr.strip()}", ERR)
            return False, {path: known[path] for path in shipped if path in known}
    say(f"reloaded udev for {len(shipped)} shipped rule(s)")
    return True, shipped
