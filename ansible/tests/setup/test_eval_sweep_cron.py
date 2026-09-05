"""Guards for the Homelab eval sweep cron (initial_setup role, tags `crons`/`evals`).

Weekly hermetic run of every evals/cases/ case, rolled into evals/history.json by
evals/trend.py. Modeled on docs-refresh.sh: same git-tree lock, same PR-based publish (that
half is covered generically for every unattended committing cron by
test_cron_scripts_publish_via_pr.py, which now includes eval-run.sh.j2 in its SCRIPTS list).

What's specific to this cron, and what this file guards:

- It exists and runs weekly, not daily -- a full hermetic sweep costs real API dollars
  (evals/README.md "Auth & fidelity"), so a tighter schedule would be a standing cost, not a
  safety margin.
- Its `job:` sets PATH and HOME explicitly, since cron inherits neither and node (via fnm),
  uv, git and gh all have to resolve without a login shell.
- The script self-gates on an empty anthropic_api_key, and the Kuma monitor self-gates on an
  empty homelab_eval_push_token -- no such SOPS secret exists yet, so both must install
  disarmed rather than red or silently non-hermetic.
"""

import json
import re

from _helpers import ANSIBLE

CRONS = (ANSIBLE / "roles/setup/initial_setup/tasks/crons.yml").read_text()
SCRIPT = (ANSIBLE / "roles/setup/initial_setup/templates/eval-run.sh.j2").read_text()
MONITORS = (
    ANSIBLE / "roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
).read_text()


def _cron_job_line(name: str) -> str:
    """The rendered `job:` value for a cron task, found by its `name:` field."""
    block = CRONS[CRONS.index(f'name: "{name}"') :]
    m = re.search(r'job: "([^"]*)"', block)
    assert m, f"cron {name!r} has no job: field"
    return m.group(1)


def test_the_cron_exists_and_runs_weekly():
    start = CRONS.index('name: "Homelab eval sweep"')
    block = CRONS[start : CRONS.index("job:", start)]
    assert 'weekday: "0"' in block, (
        "the eval sweep must run weekly, not daily — a full hermetic sweep costs real API "
        "dollars per evals/README.md"
    )
    assert "eval-run.sh" in _cron_job_line("Homelab eval sweep")


def _job_sets(var: str, job: str) -> bool:
    """Whether a cron `job:` string exports `var` ahead of the command it runs."""
    return bool(re.search(rf"(^|\s){var}=\S", job))


def test_the_cron_job_sets_path_and_home():
    job = _cron_job_line("Homelab eval sweep")
    assert _job_sets("PATH", job), (
        f"job line sets no PATH, so node/uv/git/gh may not resolve: {job!r}"
    )
    assert _job_sets("HOME", job), f"job line sets no HOME: {job!r}"


def test_the_job_sets_helper_rejects_an_unset_var():
    """The rejecting half: without it `_job_sets` could return True unconditionally."""
    assert not _job_sets("HOME", "/usr/local/bin/eval-run.sh")
    assert not _job_sets("PATH", "HOMER=1 /usr/local/bin/eval-run.sh")


def _is_gated_on_the_api_key(text: str) -> bool:
    """Whether a script skips its run when ANTHROPIC_API_KEY is empty."""
    return bool(re.search(r'if\s+\[\s+-z\s+"\$ANTHROPIC_API_KEY"\s+\];\s+then', text))


def test_the_script_is_gated_on_the_api_key():
    assert "{{ anthropic_api_key | default('') }}" in SCRIPT
    assert _is_gated_on_the_api_key(SCRIPT), (
        "the script must skip the sweep when no anthropic_api_key secret exists, or it burns "
        "a Claude Code subscription session non-hermetically every week (evals/README.md "
        "Auth & fidelity)"
    )


def test_the_api_key_gate_scan_rejects_an_ungated_script():
    """The rejecting half: without it `_is_gated_on_the_api_key` could return True on anything."""
    assert not _is_gated_on_the_api_key('echo "no gate here"')


def test_the_monitor_is_gated_on_its_token():
    assert "{% if homelab_eval_push_token | default('') %}" in MONITORS, (
        "an ungated monitor sits red from creation until the secret exists — gate it like "
        "docs-refresh.json does"
    )


def _unchanged_arm_is_safe(script: str) -> bool:
    """Whether the git-diff-empty branch after trend.py fails closed rather than reporting up.

    Unlike docs-refresh's generator, a successful hermetic run always appends one entry per
    graded case to evals/history.json (evals/trend.py's record_run/write_json), so an
    unchanged file here is never a benign "nothing to do" -- it means trend.py crashed before
    writing (a malformed combined.json, an argparse error). There is no benign arm to protect,
    unlike GENERATORS_OK's branch in docs-refresh.sh.
    """
    marker = "TREND_RC=$?"
    if marker not in script:
        return False
    start = script.index(marker)
    if "if git diff --cached --quiet; then" not in script[start:]:
        return False
    arm_start = script.index("if git diff --cached --quiet; then", start)
    arm = script[arm_start : script.index("\nfi\n", arm_start)]
    return "PUSH_STATUS=up" not in arm and "exit 1" in arm


def test_a_trend_crash_is_not_laundered_as_up():
    assert _unchanged_arm_is_safe(SCRIPT), (
        "the history-unchanged arm after trend.py must fail closed (say_failure + exit 1), "
        "not report up — a crash there would read as a clean beat, the same GENERATORS_OK "
        "failure mode docs-refresh.sh already paid for once"
    )


def test_the_unchanged_arm_scan_rejects_a_laundered_version():
    """The rejecting half: without it `_unchanged_arm_is_safe` could return True unconditionally."""
    laundered = (
        "TREND_RC=$?\n"
        "if git diff --cached --quiet; then\n"
        "  PUSH_STATUS=up\n"
        "  exit 0\n"
        "fi\n"
    )
    assert not _unchanged_arm_is_safe(laundered)


def test_push_monitor_does_not_retry():
    m = re.search(r"^  homelab-eval-sweep\.json: \|\n\s+(\{.*\})$", MONITORS, re.M)
    assert m, "homelab-eval-sweep.json entity missing from static-monitors.yaml.j2"
    entity = json.loads(re.sub(r"\{\{[^}]*\}\}", "0", m.group(1)))
    assert entity["max_retries"] == 0, (
        "a push monitor's deadline IS its retry; max_retries re-arms it and delays the alert"
    )
