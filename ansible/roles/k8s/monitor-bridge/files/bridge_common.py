"""Helpers shared verbatim between monitor-bridge's check.py and autofix-bridge's autofix.py.

Both bridges are stdlib-only Python loops that read config from the environment and push a
result to Uptime Kuma. They grew as separate files and drifted into their own copies of the
same few pure helpers — this module is the one place those bodies live now.

WHAT MAY LIVE HERE, AND WHY MOST OF THE DUPLICATION STAYS
===========================================================
This module is imported by check.py, so bridge_parsing.py's rule binds here too: a function may
live here only if it is never monkeypatched by EITHER file's test suite AND reads no
module-level name that either suite patches. `_env` and `sanitize` are the only two candidates
that clear both bars. Everything else on the surface duplication list — `log`, `push`,
`touch_heartbeat`, the urllib wrapper (`_get_json`/`_post_json` vs `_request`), and each file's
`main()` sleep loop — stays duplicated because at least one suite patches it directly, or patches
a config constant it reads:
  - check.py: `log` (test_check_gates.py), `push`/`_get_json` (dozens of sites across the
    suite), and `HEARTBEAT_FILE` — which touch_heartbeat reads (test_check.py).
  - autofix.py: `push` and `_request` (test_autofix.py's `_configure_sonarr_only` and the
    run_once tests).
Moving any of those here would leave a `monkeypatch.setattr(check, "X", ...)` or
`monkeypatch.setattr(autofix, "X", ...)` rebinding a name nothing reads, so the test would pass
against unpatched production code. See bridge_parsing.py's header for the full argument.

ENFORCED by ansible/tests/test_monitor_bridge_modules.py (bridge_common is in SPLIT_MODULES
there, same as the verdicts_*/bridge_parsing modules) for the check.py side. autofix.py's suite
has no equivalent automated guard — this docstring is what's checked before a future edit widens
what lives here.

Ship path: this file is monitor-bridge's canonical copy. autofix-bridge stages a copy of it onto
the node from here (`{{ playbook_dir }}/roles/k8s/monitor-bridge/files/bridge_common.py`), the
same cross-role pattern `host_lib.py` uses from `roles/setup/common` — never fork a second edited
copy under autofix-bridge/files/.
"""

import os


def _env(name, default):
    return os.environ.get(name, default)


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
