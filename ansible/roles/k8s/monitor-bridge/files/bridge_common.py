"""Helpers shared verbatim between monitor-bridge's check.py and autofix-bridge's autofix.py.

Both bridges are stdlib-only Python loops that read config from the environment and push a
result to Uptime Kuma. They grew as separate files and drifted into their own copies of the
same few pure helpers — this module is the one place those bodies live now.

WHAT MAY LIVE HERE, AND WHY SOME DUPLICATION STAYS
===========================================================
This module is imported by check.py, so bridge_parsing.py's rule binds here too, in a form the
`scripts/probe/test_probe_boundaries.py` precedent widens rather than a strict ban: a helper the test
suites patch directly may still live here, PROVIDED every caller reaches it *qualified* —
`bridge_common.log(...)`, never `from bridge_common import log` — since `monkeypatch.setattr`
rebinds the attribute on this module object, and only a qualified lookup re-reads that attribute
at call time. A from-import binds its own reference at import time and never sees the patch. This
is enforced by `ansible/tests/test_bridge_patch_boundary.py`, which AST-walks every test suite for
`setattr(bridge_common, "X", ...)` and asserts no non-test module reaches `X` via a from-import.
Likewise a helper that reads a module-level constant either suite patches (e.g. `HEARTBEAT_FILE`)
may live here if the constant is taken as an **argument** rather than read as a global — the
caller still defines and patches its own constant, and passes it in at call time, so the existing
`monkeypatch.setattr(check, "HEARTBEAT_FILE", ...)` keeps working. `log` and `touch_heartbeat(path)`
are the two helpers here that rely on this: `log` is qualified everywhere, and `touch_heartbeat`
takes its heartbeat path as an argument instead of reading a module-global.

`_env` and `sanitize` need neither device — they are never patched by either suite, so a plain
from-import is fine and preferred (churn-free; `_env` alone has 145 call sites in check.py).

Everything else on the surface duplication list stays duplicated because unifying it is a
behaviour change, not a patching-boundary problem: `push`, the urllib wrapper
(`_get_json`/`_post_json` vs `_request`), and each file's `main()` sleep loop have genuinely
drifted signatures (`check.push(token, ok, msg)` vs `autofix.push(ok, msg)`) and dozens of direct
patch sites apiece. See bridge_parsing.py's header for the full argument on why a patched name
can't just move without qualification or argument-passing.

ENFORCED by ansible/tests/test_monitor_bridge_modules.py (bridge_common is in SPLIT_MODULES
there, same as the verdicts_*/bridge_parsing modules) for the check.py side, and by
ansible/tests/test_bridge_patch_boundary.py for the qualified-access rule across both bridges.

Ship path: this file is monitor-bridge's canonical copy. autofix-bridge stages a copy of it onto
the node from here (`{{ playbook_dir }}/roles/k8s/monitor-bridge/files/bridge_common.py`), the
same cross-role pattern `host_lib.py` uses from `roles/setup/common` — never fork a second edited
copy under autofix-bridge/files/.
"""

import os
import time


def _env(name, default):
    return os.environ.get(name, default)


def log(*args):
    """Print a bracketed-timestamp log line.

    The bracketed stamp is LOCAL time (America/Chicago via the container's TZ env), not UTC —
    see the monitor-bridge CLAUDE.md's "bracketed log timestamps" trap for the incident that
    came from reading it as UTC. Callers must reach this qualified as `bridge_common.log(...)`;
    see this module's header.
    """
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


def touch_heartbeat(path):
    """Write the current time to `path`, the liveness-probe heartbeat file.

    Takes the path as an argument rather than reading a module-level constant, so each caller's
    own `HEARTBEAT_FILE` (which its test suite patches) still governs where this writes — see
    this module's header.
    """
    try:
        with open(path, "w") as fh:
            fh.write("%s\n" % time.time())
    except OSError as e:  # best-effort like push(); never crash the loop
        log("WARN: heartbeat write failed:", e)


def sanitize(s, maxlen=120):
    """Neutralize adversary-controlled text before it enters a Discord-bound alert msg.

    Release titles, indexer names, n8n workflow names and *arr queue items are
    attacker-influenced — a poisoned indexer/release is the very thing several checks exist to
    catch. Kuma forwards the msg to Discord, which renders @mentions and markdown, so collapse
    whitespace, defuse '@' (which forms @everyone/@here/user pings) and backticks, and cap the
    length.
    """
    s = "?" if s is None else str(s)
    s = " ".join(s.split())
    s = s.replace("@", "(at)").replace("`", "'")
    if len(s) > maxlen:
        s = s[: maxlen - 3] + "..."
    return s
