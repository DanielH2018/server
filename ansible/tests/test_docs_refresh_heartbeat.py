"""Guards for the docs-refresh failure report (initial_setup role, tag `crons`).

The script published nothing on 2026-08-27 and every signal it had read healthy: it ran,
`build_docs.py` succeeded, and `build-info.json` stamped `generators: ok` from the very run whose
commit a prek hook rejected. Its `NO DEADMAN` block is still right that a *stopped* cron shows up
on the page — a deadman detects silence, and neither of this script's two failures was silence.
What these guard is the failure report added to close that, and the couplings it depends on:

- **The Kuma deadline must straddle the cron period.** Below it the monitor fires DOWN on a run
  that merely took a minute; at more than twice it, a whole missed run goes unreported. 46800
  sits between a 12h period and 24h.
- **Exactly one `trap ... EXIT`.** A second trap on the same signal REPLACES the first, so the
  temp-file cleanup that used to own EXIT would have silently disabled the push.
- **The status defaults to down.** Eleven exit paths, one of which is a bare crash.
"""

from __future__ import annotations

import json
import re

from _helpers import ANSIBLE

MONITORS = (
    ANSIBLE / "roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
).read_text()
SCRIPT = (
    ANSIBLE / "roles/setup/initial_setup/templates/docs-refresh.sh.j2"
).read_text()
CRONS = (ANSIBLE / "roles/setup/initial_setup/tasks/crons.yml").read_text()
ROTATION = (ANSIBLE / "secret_rotation.yml").read_text()


def _monitor_entity() -> dict:
    """The rendered Docs Refresh entity, read out of the template by filename key.

    Parsed from the raw template rather than a Jinja render, matching
    test_remember_logs_health.py: every field asserted here is a literal.
    """
    m = re.search(r"^  docs-refresh\.json: \|\n\s+(\{.*\})$", MONITORS, re.M)
    assert m, "docs-refresh.json entity missing from static-monitors.yaml.j2"
    return json.loads(re.sub(r"\{\{[^}]*\}\}", "0", m.group(1)))


def _cron_period_seconds() -> int:
    """Seconds between two docs-refresh runs, read from its `hour:` field in crons.yml.

    Raises on any form other than the comma-separated list this cron uses, so a future cadence
    cannot quietly resolve to a period that makes the assertions below vacuous.
    """
    block = CRONS[CRONS.index('name: "Refresh generated docs"') :]
    m = re.search(r'hour: "([^"]+)"', block)
    assert m, "docs-refresh cron has no hour: field"
    spec = m.group(1)
    assert re.fullmatch(r"\d+(,\d+)*", spec), (
        f"unhandled cron hour spec {spec!r} — teach this helper the form"
    )
    hours = sorted(int(h) for h in spec.split(","))
    assert len(hours) >= 2, "a single daily run needs a different period derivation"
    gaps = [b - a for a, b in zip(hours, hours[1:])]
    gaps.append(24 - hours[-1] + hours[0])
    return min(gaps) * 3600


def test_kuma_deadline_exceeds_the_cron_period():
    interval = _monitor_entity()["interval"]
    period = _cron_period_seconds()
    assert interval > period, (
        f"Kuma interval {interval}s does not exceed the {period}s cron period — the monitor "
        f"fires DOWN on a run that merely takes a minute. Move the interval and the cron "
        f"hour: field together."
    )


def test_a_whole_missed_run_still_reports_down():
    """The other half of the pair: an interval nobody can breach reports nothing.

    Without this, `interval > period` is satisfied by any absurdly large number — 90000, the
    value every daily sibling uses and the obvious thing to copy, would let a complete missed
    run pass unnoticed.
    """
    interval = _monitor_entity()["interval"]
    period = _cron_period_seconds()
    assert interval < 2 * period, (
        f"Kuma interval {interval}s is at least two cron periods ({period}s) — a whole missed "
        f"run would go unreported, which is the failure this monitor exists to catch."
    )


def test_push_monitor_does_not_retry():
    assert _monitor_entity()["max_retries"] == 0, (
        "a push monitor's deadline IS its retry; max_retries re-arms it and delays the alert"
    )


def test_the_monitor_is_gated_on_its_token():
    assert "{% if docs_refresh_push_token | default('') %}" in MONITORS, (
        "an ungated monitor sits red from creation until the secret exists — gate it like "
        "manifest-prune-check.json does"
    )


def test_the_script_skips_its_push_until_the_token_exists():
    assert "{{ docs_refresh_push_token | default('') }}" in SCRIPT
    assert '[ -n "$DOCS_REFRESH_PUSH_TOKEN" ] || return 0' in SCRIPT, (
        "the script must skip the push when the token is empty, or a host without the secret "
        "logs a push failure every run"
    )


def test_exactly_one_exit_trap():
    """A second `trap ... EXIT` replaces the first rather than adding to it.

    The temp-file cleanup owned EXIT before the push was added. Two traps here means one of the
    two jobs is silently not happening, and which one depends on source order.
    """
    traps = re.findall(r"^\s*trap\s+.*\bEXIT\b", SCRIPT, re.M)
    assert len(traps) == 1, (
        f"expected exactly one EXIT trap, found {len(traps)}: {traps}"
    )


def test_the_trap_is_installed_before_the_first_exit_path():
    assert SCRIPT.index("trap on_exit EXIT") < SCRIPT.index('cd "$REPO"'), (
        "an exit path above the trap reports nothing — the cd and flock failures are the "
        "earliest two"
    )


def test_the_status_defaults_to_down():
    """Fail closed: anything that does not explicitly say it is fine reports down.

    This is what covers a crash, a `set -u` abort, and any exit path added later that forgets to
    set a status — which is the realistic way this regresses.
    """
    assert re.search(r"^PUSH_STATUS=down$", SCRIPT, re.M), (
        "PUSH_STATUS must default to down; defaulting to up reproduces the exact failure this "
        "was added for"
    )


def test_a_generator_failure_is_not_laundered_by_a_successful_publish():
    """`build_docs.py` failing does not stop the run, so the final publish must not report up.

    Without the GENERATORS_OK flag the successful PR overwrites the degradation and the monitor
    reads clean while the published pages are incomplete.
    """
    assert "GENERATORS_OK=0" in SCRIPT
    assert re.search(r'if \[ "\$GENERATORS_OK" -eq 1 \]', SCRIPT), (
        "the terminal status must consult GENERATORS_OK, or a failing generator rides out on a "
        "successful publish"
    )


def test_the_token_is_registered_for_rotation():
    assert "docs_refresh_push_token:" in ROTATION, (
        "run scripts/secrets_mgmt/secret_rotation.py sync — an unregistered secret is never "
        "due and never rotated"
    )
