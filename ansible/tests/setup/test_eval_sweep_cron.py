"""Guards for the Homelab eval sweep cron (initial_setup role, tags `crons`/`evals`).

Weekly hermetic run of every evals/cases/ case, rolled into evals/history.json by
evals/trend.py. Modeled on docs-refresh.sh: same git-tree lock, same PR-based publish (that
half is covered generically for every unattended committing cron by
test_cron_scripts_publish_via_pr.py, which includes eval-run.sh.j2 in its SCRIPTS list).

What's specific to this cron, and what this file guards:

- It exists and runs weekly, not daily -- a full hermetic sweep costs real API dollars
  (evals/README.md "Auth & fidelity"), so a tighter schedule would be a standing cost, not a
  safety margin.
- Its `job:` sets PATH and HOME explicitly, since cron inherits neither and node (via fnm),
  uv, git and gh all have to resolve without a login shell.
- The script self-gates on an empty anthropic_api_key, and the Kuma monitor self-gates on an
  empty homelab_eval_push_token -- a host without those SOPS secrets must install both
  disarmed rather than red or silently non-hermetic.
- The key reaches the script through a 0600 env file it sources, never through its own body
  (#4607), and a run with no env file is the disarmed no-op that reports UP.
"""

import re

from _helpers import ANSIBLE, ROLES
from _kuma_monitors import entity, tile_is_gated_on
from _shell_render import render_shell_script, rendered_shell_text
from lib import yaml_fast
from lib.ansible_jinja_env import render_template
from lib.proc_testing import fake_bin, run
from validate.shell_templates import template_context

CRONS = (ANSIBLE / "roles/setup/initial_setup/tasks/crons.yml").read_text()
SCRIPT = rendered_shell_text("setup", "initial_setup", "eval-run.sh.j2")

TILE = "homelab-eval-sweep.json"
TOKEN_VAR = "homelab_eval_push_token"
# A value no secret holds, so a render that carries it carries THIS variable's value.
KEY_SENTINEL = "eval-run-render-sentinel"


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


def _render_env_file(overrides: dict) -> str:
    path = ROLES / "setup/initial_setup/templates/eval-run.env.j2"
    return render_template(path, template_context(path, overrides=overrides))


def test_the_api_key_renders_into_the_env_file_and_not_the_script():
    # Both halves render at a SENTINEL. At inventory values an unset secret renders as the same
    # empty string a wrong variable name would, so only a render at a value this variable alone
    # holds separates the two (#3202). The script half is #4607: a key inside the script is
    # printed by every grep, cat or diff over it.
    overrides = {"anthropic_api_key": KEY_SENTINEL}
    script = render_shell_script(
        "setup", "initial_setup", "eval-run.sh.j2", overrides=overrides
    )
    assert KEY_SENTINEL not in script, (
        "eval-run.sh renders anthropic_api_key into its own body; source it from the env file"
    )
    assert f"ANTHROPIC_API_KEY='{KEY_SENTINEL}'" in _render_env_file(overrides), (
        "the env file does not set ANTHROPIC_API_KEY from anthropic_api_key, so the sweep "
        "would run with no key or with somebody else's"
    )


def _env_file_task(state: str) -> dict:
    tasks = [
        t
        for t in yaml_fast.safe_load(CRONS)
        if "initial_setup_eval_run_env_file" in str(t)
        and (t.get("ansible.builtin.file", {}).get("state") == "absent")
        == (state == "absent")
    ]
    assert len(tasks) == 1, (
        f"expected one {state} task for the eval-run env file: {tasks}"
    )
    return tasks[0]


def test_the_env_file_task_keeps_the_key_private():
    task = _env_file_task("present")
    assert task["no_log"] is True, (
        "the env-file task renders the key; without no_log a --diff prints it"
    )
    module = task["ansible.builtin.template"]
    assert module["mode"] == "0600"
    assert module["owner"] == "{{ sys_user }}", (
        "the cron runs as sys_user, which must read it"
    )


def test_the_env_file_is_removed_when_no_key_is_configured():
    """The way out: a key dropped from SOPS must not stay armed on disk."""
    when = _env_file_task("absent")["when"]
    assert "anthropic_api_key" in when and "length == 0" in when, when


def _run_script(tmp_path, env_file_text: str | None):
    """Run the rendered script whole, with `logger` stubbed to print what it would log.

    `sys_user` points at a home that does not exist, so a run that gets past the key gate stops
    at `cd "$REPO"` before any git, gh or paid API call. The push token renders empty so the
    exit hook never pushes to the live Kuma.
    """
    env_file = tmp_path / "eval-run.env"
    if env_file_text is not None:
        env_file.write_text(env_file_text)
    script = tmp_path / "eval-run.sh"
    script.write_text(
        render_shell_script(
            "setup",
            "initial_setup",
            "eval-run.sh.j2",
            overrides={
                "initial_setup_eval_run_env_file": str(env_file),
                "homelab_eval_push_token": "",
                "sys_user": "eval-run-test-no-such-user",
            },
        )
    )
    return run(
        ["bash", str(script)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        stub_bin=fake_bin(tmp_path / "bin", logger='printf "%s\\n" "$*"\n'),
    )


def test_a_run_with_no_env_file_is_a_no_op_reporting_up(tmp_path):
    result = _run_script(tmp_path, env_file_text=None)
    assert result.returncode == 0, result.stderr
    assert (
        "status=up eval-run: skipped, no anthropic_api_key secret configured"
        in result.stdout
    ), result.stdout


def test_a_run_with_a_key_in_the_env_file_gets_past_the_gate(tmp_path):
    """The red half: without it the no-op test above passes on a script that never arms."""
    result = _run_script(tmp_path, env_file_text=f"ANTHROPIC_API_KEY={KEY_SENTINEL}\n")
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
