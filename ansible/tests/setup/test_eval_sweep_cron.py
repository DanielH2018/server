"""Guards for the Homelab eval sweep timer (the eval-sweep component of homelab-daniel-box).

Weekly hermetic run of every evals/cases/ case, rolled into evals/history.json by
evals/trend.py. Modeled on docs-refresh: same git-tree lock, same PR-based publish (that
half is covered generically for every unattended committing job by
test_cron_scripts_publish_via_pr.py, which includes eval-run.sh.j2 in its SCRIPTS list).

What's specific to this unit, and what this file guards:

- It runs weekly, not daily -- a full hermetic sweep costs real API dollars
  (evals/README.md "Auth & fidelity"), so a tighter schedule would be a standing cost, not a
  safety margin.
- The service's PATH, HOME and credentials are held by hosts/lib/tests/test_eval_sweep_component.py.
- The script self-gates on an empty anthropic_api_key, and the Kuma monitor self-gates on an
  empty homelab_eval_push_token -- a host without those SOPS secrets must install both
  disarmed rather than red or silently non-hermetic.
- Both secrets reach the script through systemd credentials, never through its own body
  (#4607), and a run with no credential is the disarmed no-op that reports UP.
"""

import re

from _helpers import ANSIBLE
from _kuma_monitors import entity, tile_is_gated_on
from _shell_render import render_shell_script, rendered_shell_text
from lib import yaml_fast
from lib.proc_testing import fake_bin, run
from lib.repo_paths import REPO

_CHECK_LIB_PATH = "/usr/lib/homelab/homelab-check-lib.sh"
_CHECK_LIB = (
    ANSIBLE / "roles" / "setup" / "initial_setup" / "files" / "homelab-check-lib.sh"
)

COMPONENT = REPO / "hosts/components/eval-sweep"
TIMER = (COMPONENT / "units/homelab-eval-sweep.timer").read_text()
SPEC = yaml_fast.safe_load((COMPONENT / "component.yml").read_text())
SCRIPT = rendered_shell_text("components", "eval-sweep", "eval-run.sh.j2")

TILE = "homelab-eval-sweep.json"
TOKEN_VAR = "homelab_eval_push_token"
KEY_VAR = "anthropic_api_key"
# A value no secret holds, so a render that carries it carries THIS variable's value.
KEY_SENTINEL = "eval-run-render-sentinel"


def test_the_timer_runs_weekly_on_sunday_at_two():
    calendar = re.search(r"^OnCalendar=(.*)$", TIMER, re.M)
    assert calendar, "the eval sweep timer has no OnCalendar="
    assert calendar.group(1).strip() == "Sun *-*-* 02:00:00", (
        "the eval sweep must run weekly, not daily -- a full hermetic sweep costs real API "
        "dollars per evals/README.md"
    )
    assert "homelab-eval-sweep.timer" in SPEC["enable"]


def _is_gated_on_the_api_key(text: str) -> bool:
    """Whether a script skips its run when ANTHROPIC_API_KEY is empty."""
    return bool(re.search(r'if\s+\[\s+-z\s+"\$ANTHROPIC_API_KEY"\s+\];\s+then', text))


def test_the_script_holds_neither_secret():
    # Rendered at a SENTINEL. At inventory values an unset secret renders as the same empty
    # string a wrong variable name would, so only a render at a value this variable alone holds
    # separates the two (#3202). A key inside the script is printed by every grep, cat or diff
    # over it (#4607).
    script = render_shell_script(
        "components",
        "eval-sweep",
        "eval-run.sh.j2",
        overrides={KEY_VAR: KEY_SENTINEL, TOKEN_VAR: KEY_SENTINEL},
    )
    assert KEY_SENTINEL not in script, (
        "eval-run.sh renders a secret into its own body; read it from $CREDENTIALS_DIRECTORY"
    )
    assert f"$CRED_DIR/{KEY_VAR}" in script
    assert f"$CRED_DIR/{TOKEN_VAR}" in script


def _run_script(tmp_path, key_text: str | None):
    """Run the rendered script whole, with `logger` stubbed to print what it would log.

    `sys_user` points at a home that does not exist, so a run that gets past the key gate stops
    at `cd "$REPO"` before any git, gh or paid API call. The push token credential is absent, so
    the exit hook never pushes to the live Kuma.

    The caller's environment carries another key, so the credential file is the ONLY place a key
    can come from. A script that kept an inherited key gets past the gate with no credential.
    """
    creds = tmp_path / "creds"
    creds.mkdir()
    if key_text is not None:
        (creds / KEY_VAR).write_text(key_text)
    script = tmp_path / "eval-run.sh"
    body = render_shell_script(
        "components",
        "eval-sweep",
        "eval-run.sh.j2",
        overrides={"sys_user": "eval-run-test-no-such-user"},
    )
    # The script sources the check library from the host's install path, which CI lacks, so
    # point it at the role's copy; the `write_check_textfile` its exit hook calls then exists.
    assert _CHECK_LIB_PATH in body
    script.write_text(body.replace(_CHECK_LIB_PATH, str(_CHECK_LIB)))
    return run(
        ["bash", str(script)],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CREDENTIALS_DIRECTORY": str(creds),
            "ANTHROPIC_API_KEY": "inherited-from-the-caller",
        },
        stub_bin=fake_bin(tmp_path / "bin", logger='printf "%s\\n" "$*"\n'),
    )


def test_a_run_with_no_credential_is_a_no_op_reporting_up(tmp_path):
    result = _run_script(tmp_path, key_text=None)
    assert result.returncode == 0, result.stderr
    assert (
        "status=up eval-run: skipped, no anthropic_api_key secret configured"
        in result.stdout
    ), result.stdout


def test_a_run_with_an_empty_credential_is_a_no_op_reporting_up(tmp_path):
    result = _run_script(tmp_path, key_text="")
    assert result.returncode == 0, result.stderr
    assert "status=up eval-run: skipped, no anthropic_api_key" in result.stdout


def test_a_run_with_a_key_credential_gets_past_the_gate(tmp_path):
    """The red half: without it the no-op tests above pass on a script that never arms."""
    result = _run_script(tmp_path, key_text=KEY_SENTINEL)
    assert "skipped, no anthropic_api_key" not in result.stdout, result.stdout
    assert "status=down eval-run: /home/eval-run-test-no-such-user/server missing" in (
        result.stdout
    ), result.stdout


def test_the_script_is_gated_on_the_api_key():
    assert _is_gated_on_the_api_key(SCRIPT), (
        "the script must skip the sweep when no anthropic_api_key secret exists, or it burns "
        "a Claude Code subscription session non-hermetically every week (evals/README.md "
        "Auth & fidelity)"
    )


def test_the_api_key_gate_scan_rejects_an_ungated_script():
    """The rejecting half: without it `_is_gated_on_the_api_key` could return True on anything."""
    assert not _is_gated_on_the_api_key('echo "no gate here"')


def test_the_monitor_is_gated_on_its_token():
    assert tile_is_gated_on(TILE, TOKEN_VAR), (
        "an ungated monitor sits red from creation until the secret exists — gate it like "
        f"docs-refresh.json does. The tile renders with {TOKEN_VAR} empty."
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
    assert entity(TILE, TOKEN_VAR)["max_retries"] == 0, (
        "a push monitor's deadline IS its retry; max_retries re-arms it and delays the alert"
    )
