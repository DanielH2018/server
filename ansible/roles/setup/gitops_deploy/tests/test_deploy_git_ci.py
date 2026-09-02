"""The CI gate: the tip must be green before anything is merged or deployed.

`ci_verdict` folds GitHub's check-runs into pass / fail / pending, and the shape that matters
is fail-closed: an absent run, an unfinished run and a cancelled run are all "no verdict",
never a pass. The required context names are read from defaults/main.yml and checked against
ci.yml's job names, because a renamed job holds the verdict at pending forever.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_git_ci.py

import pathlib

import yaml

from deploy_git import ci_verdict, next_action

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


# ── CI gate ───────────────────────────────────────────────────────────────────────────────────

_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_GITOPS_DEFAULTS = pathlib.Path(__file__).parents[1] / "defaults" / "main.yml"


def _ci_job_names() -> set[str]:
    jobs = yaml.safe_load(_CI_YML.read_text())["jobs"]
    return {job["name"] for job in jobs.values() if job.get("name")}


def _required_contexts() -> list[str]:
    defaults = yaml.safe_load(_GITOPS_DEFAULTS.read_text())
    return defaults["gitops_deploy_ci_contexts"]


def test_required_ci_contexts_are_real_ci_yml_job_names():
    """`gitops_deploy_ci_contexts` holds GitHub check-run names, which are ci.yml's job `name:`.

    The string was hand-copied into ci.yml, defaults/main.yml and this file, and the test
    asserted against its own copy — so it could not see drift in the other two. A rename in
    ci.yml holds `ci_verdict` at `pending` forever: `fetch_ci_verdict` finds no run of the
    required name, `next_action` returns `ci_pending`, and the host parks until the 6h
    behind-origin watchdog pages.
    """
    names = _ci_job_names()
    contexts = _required_contexts()
    assert names, "parsed no job names out of %s" % _CI_YML
    assert contexts, "parsed no gitops_deploy_ci_contexts out of %s" % _GITOPS_DEFAULTS
    assert all(isinstance(c, str) and c.strip() for c in contexts), contexts

    missing = sorted(set(contexts) - names)
    assert not missing, (
        "gitops_deploy_ci_contexts names check-runs that no ci.yml job produces, so the CI "
        "gate can never go green: %s (ci.yml jobs: %s)" % (missing, sorted(names))
    )


_PREK = _required_contexts()[0]
# Deliberately NOT frozenset(_required_contexts()): the tests below feed a single check-run, so
# adding a second required context would fail them for the wrong reason (the second name reports
# nothing, so the verdict is `pending`). The multi-context case has its own test with its own
# names; test_required_ci_contexts_are_real_ci_yml_job_names is the one reader of the whole list.
_REQUIRED = frozenset({_PREK})


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_ci_verdict_passes_when_required_context_is_green():
    assert ci_verdict([_run(_PREK)], _REQUIRED) == "pass"


def test_ci_verdict_fails_on_failure():
    assert ci_verdict([_run(_PREK, conclusion="failure")], _REQUIRED) == "fail"
    assert ci_verdict([_run(_PREK, conclusion="timed_out")], _REQUIRED) == "fail"


def test_ci_verdict_pending_while_still_running():
    assert ci_verdict(
        [_run(_PREK, status="in_progress", conclusion=None)], _REQUIRED
    ) == ("pending")
    assert (
        ci_verdict([_run(_PREK, status="queued", conclusion=None)], _REQUIRED)
        == "pending"
    )


def test_ci_verdict_pending_when_the_context_has_not_reported_at_all():
    # A SHA pushed seconds ago has no check-runs yet. Absence must never read as success.
    assert ci_verdict([], _REQUIRED) == "pending"
    assert ci_verdict([_run("some other job")], _REQUIRED) == "pending"


def test_ci_verdict_treats_cancelled_as_no_verdict_not_failure():
    # ci.yml sets concurrency cancel-in-progress on github.ref, so two pushes in quick succession
    # CANCEL the first run. That means "no verdict for this SHA", not "this SHA is bad" — mapping
    # it to a failure would page on an ordinary back-to-back push.
    assert ci_verdict([_run(_PREK, conclusion="cancelled")], _REQUIRED) == "pending"
    assert ci_verdict([_run(_PREK, conclusion="stale")], _REQUIRED) == "pending"


def test_ci_verdict_skipped_and_neutral_count_as_green():
    assert ci_verdict([_run(_PREK, conclusion="skipped")], _REQUIRED) == "pass"
    assert ci_verdict([_run(_PREK, conclusion="neutral")], _REQUIRED) == "pass"


def test_ci_verdict_failure_wins_over_a_second_run_of_the_same_name():
    # One name can carry several runs (a re-run, or push + pull_request on the same SHA).
    # The worst outcome has to win, or a green re-run would paper over a red one.
    runs = [_run(_PREK), _run(_PREK, conclusion="failure")]
    assert ci_verdict(runs, _REQUIRED) == "fail"
    assert ci_verdict(list(reversed(runs)), _REQUIRED) == "fail"


def test_ci_verdict_pending_when_one_run_of_the_name_is_unfinished():
    runs = [_run(_PREK), _run(_PREK, status="in_progress", conclusion=None)]
    assert ci_verdict(runs, _REQUIRED) == "pending"


def test_ci_verdict_all_of_several_required_contexts_must_be_green():
    required = frozenset({_PREK, "renovate config validator"})
    assert ci_verdict([_run(_PREK)], required) == "pending"
    assert (
        ci_verdict([_run(_PREK), _run("renovate config validator")], required) == "pass"
    )


def test_ci_verdict_empty_required_set_disarms_the_gate():
    # An un-templated config.env leaves CI_CONTEXTS empty; that host must keep its old behaviour
    # rather than deferring every tick forever.
    assert ci_verdict([], frozenset()) == "pass"
    assert ci_verdict([_run(_PREK, conclusion="failure")], frozenset()) == "pass"


def test_next_action_defers_when_ci_has_not_finished():
    assert next_action("aaa", "bbb", None, ci="pending") == "ci_pending"


def test_next_action_refuses_to_deploy_a_red_tip():
    assert next_action("aaa", "bbb", None, ci="fail") == "ci_failed"


def test_next_action_deploys_when_ci_is_green():
    assert next_action("aaa", "bbb", None, ci="pass") == "deploy"


def test_next_action_defaults_to_deploying_when_no_ci_verdict_is_supplied():
    # Back-compat: every existing caller and test omits `ci`, and must still deploy.
    assert next_action("aaa", "bbb", None) == "deploy"


def test_ci_never_overrides_the_earlier_short_circuits():
    # dirty / noop / skip_hold all outrank the CI gate: a red tip we were never going to deploy
    # must not start reporting itself as a CI failure.
    assert next_action("aaa", "bbb", None, dirty=True, ci="fail") == "dirty"
    assert next_action("aaa", "aaa", None, ci="fail") == "noop"
    assert next_action("aaa", "bad", "bad", ci="fail") == "skip_hold"
    assert next_action("aaa", "bbb", None, origin_ahead=False, ci="fail") == "noop"
