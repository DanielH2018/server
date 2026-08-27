"""What the tick does once it has decided: auto-deploy eligibility, the CI gate, rollback.

Auto-deploy is the highest-stakes decision here — an ineligible role deployed without a working
gate is a change nobody watched land. The CI gate and the rollback call site are what stand
between a red build and a live estate, and run()'s timeout must kill the whole process group or
a wedged child outlives the tick that spawned it.
"""

# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py
import ast
import os
import pathlib
import subprocess
import sys
import time

import pytest
import yaml

from deploy_logic import (
    ChangeSet,
    services_from_changed_paths,
    next_action,
    is_image_only_diff,
    k8s_remediation,
    shared_module_consumers,
    split_k8s_auto_deploy,
    ci_verdict,
    declared_denylist,
    declares_snapshot_claims,
    rollback_volume_revert_note,
    SHARED_K8S_ROLES,
    k8s_role_paths,
)


# ── k8s auto-deploy: the diff-shape predicate ───────────────────────────────────────────────
_SPEEDTEST_DEFAULTS = "ansible/roles/k8s/speedtest/defaults/main.yml"


def _diff(*lines: str) -> str:
    """A unified diff for _SPEEDTEST_DEFAULTS carrying the given changed lines."""
    header = f"--- a/{_SPEEDTEST_DEFAULTS}\n+++ b/{_SPEEDTEST_DEFAULTS}\n@@ -2 +2 @@\n"
    return header + "".join(line + "\n" for line in lines)


def test_image_only_diff_accepts_a_pure_image_bump():
    assert is_image_only_diff(
        _diff(
            "-speedtest_k8s_image: openspeedtest/latest:v2.0.4",
            "+speedtest_k8s_image: openspeedtest/latest:v2.0.5",
        )
    )


def test_image_only_diff_rejects_a_bundled_non_image_line():
    assert not is_image_only_diff(
        _diff(
            "-speedtest_k8s_image: openspeedtest/latest:v2.0.4",
            "+speedtest_k8s_image: openspeedtest/latest:v2.0.5",
            "-speedtest_k8s_replicas: 1",
            "+speedtest_k8s_replicas: 2",
        )
    )


def test_image_only_diff_ignores_file_headers_not_content():
    # `--- a/...` / `+++ b/...` start with -/+ but are metadata, not changed lines.
    assert is_image_only_diff(
        _diff("-speedtest_k8s_image: a:1", "+speedtest_k8s_image: a:2")
    )


def test_image_only_diff_rejects_an_empty_diff():
    # Nothing to prove -> fail closed, so an unreadable/empty git diff defers.
    assert not is_image_only_diff("")


def test_image_only_diff_rejects_a_header_only_diff():
    assert not is_image_only_diff(
        f"--- a/{_SPEEDTEST_DEFAULTS}\n+++ b/{_SPEEDTEST_DEFAULTS}\n"
    )


def test_image_only_diff_rejects_a_commented_out_image_line():
    assert not is_image_only_diff(
        _diff("-# speedtest_k8s_image: a:1", "+# speedtest_k8s_image: a:2")
    )


def test_image_only_diff_accepts_a_digest_bump():
    # The 18 mutable-tag digest pins are the population the digest automerge rule targets.
    assert is_image_only_diff(
        _diff(
            "-littlelink_k8s_image: littlelink:latest@sha256:aaa",
            "+littlelink_k8s_image: littlelink:latest@sha256:bbb",
        )
    )


# ── k8s auto-deploy: the eligibility split ──────────────────────────────────────────────────
def _split(
    paths,
    *,
    denylist=frozenset(),
    pilot=frozenset(),
    enabled=True,
    image_only=True,
    max_per_tick=0,
    claim_services=(),
    max_claim_services_per_tick=0,
):
    cs = services_from_changed_paths(paths)
    return split_k8s_auto_deploy(
        cs,
        paths,
        denylist=denylist,
        pilot=pilot,
        enabled=enabled,
        image_only=lambda _svc: image_only,
        max_per_tick=max_per_tick,
        declares_claims=lambda svc: svc in set(claim_services),
        max_claim_services_per_tick=max_claim_services_per_tick,
    )


def test_split_k8s_promotes_an_image_only_bump():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == set()


def test_split_k8s_disabled_reproduces_todays_behaviour_exactly():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"}, enabled=False)
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def _defaults_for(svc):
    return f"ansible/roles/k8s/{svc}/defaults/main.yml"


def test_split_k8s_caps_how_many_services_one_tick_takes_on():
    # The promoted set shares ONE ansible-playbook run and one K8S_DEPLOY_TIMEOUT_S, and a
    # timeout git-resets the whole merged range — so an uncapped tick can discard four good
    # image bumps because the fifth failed to roll out.
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss", "sonarr", "radarr")]
    cs = _split(paths, max_per_tick=2)
    assert len(cs.k8s_deploy) == 2
    # The surplus stays in cs.k8s, which defer-and-alerts — so it reaches the operator as a
    # Discord message naming the services to deploy by hand. It is NOT retried automatically:
    # the ff-merge precedes the deploy, so the next tick sees local == origin and noops. This
    # assertion covers the partition only; nothing here should be read as a retry guarantee.
    assert cs.k8s == {"speedtest", "freshrss", "sonarr", "radarr"} - cs.k8s_deploy


def test_split_k8s_cap_is_deterministic():
    # Same input, same promotion — otherwise which bumps land depends on set iteration order.
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss", "sonarr", "radarr")]
    assert _split(paths, max_per_tick=2).k8s_deploy == (
        _split(paths, max_per_tick=2).k8s_deploy
    )


def test_split_k8s_cap_of_zero_promotes_everything_eligible():
    paths = [_defaults_for(s) for s in ("speedtest", "freshrss")]
    assert _split(paths, max_per_tick=0).k8s_deploy == {"speedtest", "freshrss"}


def test_split_k8s_never_promotes_a_denylisted_service():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"speedtest"})
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_rejects_a_non_image_diff():
    cs = _split([_SPEEDTEST_DEFAULTS], denylist={"traefik"}, image_only=False)
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_blocks_a_service_with_a_second_changed_path():
    # Clean image bump, but the same push also edits the role's tasks/ — deploying would apply
    # an unsoaked structural change alongside it.
    cs = _split(
        [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/speedtest/tasks/main.yml"],
        denylist={"traefik"},
    )
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}


def test_split_k8s_pilot_scope_restricts_eligibility():
    paths = [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/littlelink/defaults/main.yml"]
    cs = _split(paths, denylist={"traefik"}, pilot={"speedtest"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == {"littlelink"}


def test_split_k8s_empty_pilot_means_the_denylist_governs():
    # Slice 3 (2026-08-16) cleared the pilot list. An empty pilot must mean "everything not
    # denylisted", never "nothing" — the opposite reading of the same falsy value, and the one
    # that would silently disarm the feature instead of widening it.
    paths = [_SPEEDTEST_DEFAULTS, _defaults_for("littlelink"), _defaults_for("sonarr")]
    cs = _split(paths, denylist={"sonarr"}, pilot=frozenset())
    assert cs.k8s_deploy == {"speedtest", "littlelink"}
    assert cs.k8s == {"sonarr"}


def test_split_k8s_denies_the_services_the_pilot_used_to_mask():
    # These six sat outside the denylist only because the pilot named neither them nor anything
    # else; each matches an exclusion class the design already publishes. Clearing the pilot
    # without adding them would have armed all six at once.
    masked = ("qbittorrent", "bazarr", "tdarr", "livesync", "valheim", "valheim-stats")
    cs = _split([_defaults_for(s) for s in masked], denylist=set(masked))
    assert cs.k8s_deploy == set()
    assert cs.k8s == set(masked)


def test_split_k8s_defers_when_the_tick_also_carries_docker_services():
    # main()'s k8s branch returns before the Docker deploy + health gate, so promoting here
    # would silently skip them. Defer instead.
    paths = [
        _SPEEDTEST_DEFAULTS,
        "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
    ]
    cs = _split(paths, denylist={"traefik"})
    assert cs.k8s_deploy == set()
    assert cs.k8s == {"speedtest"}
    assert cs.services == {"dozzle"}


def test_split_k8s_combined_push_deploys_eligible_defers_denylisted():
    paths = [_SPEEDTEST_DEFAULTS, "ansible/roles/k8s/traefik/defaults/main.yml"]
    cs = _split(paths, denylist={"traefik"})
    assert cs.k8s_deploy == {"speedtest"}
    assert cs.k8s == {"traefik"}


# ── deploy.yml's imported task files must reach the classifier ────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _task_imports(playbook) -> set[str]:
    """Every repo-relative task file a parsed playbook imports or includes.

    Fails closed on an argument shape it does not understand, the same bias
    `declared_denylist` takes: silently skipping one would let a converted call site drop out of
    the set while `assert imports` still passed on the remaining entries — the vacuous pass this
    guard exists to prevent, moved one level up into the parser.
    """
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.split(".")[-1] in (
                    "import_tasks",
                    "include_tasks",
                ):
                    found.add("ansible/" + _import_path(key, value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(playbook)
    return found


def _import_path(key: str, value) -> str:
    """The task file one import_tasks/include_tasks argument names, in either accepted form."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # `file:` is the documented key; `_raw_params` is what the free-form string becomes when
        # it is written alongside other options.
        for option in ("file", "_raw_params"):
            if isinstance(value.get(option), str):
                return value[option]
    raise AssertionError(
        "cannot read the task file out of `%s: %r` — teach _import_path this shape rather than "
        "letting the import drop silently out of the guard" % (key, value)
    )


def _deploy_yml_task_imports() -> set[str]:
    return _task_imports(
        yaml.safe_load((_REPO_ROOT / "ansible" / "deploy.yml").read_text())
    )


def test_task_import_parsing_reads_the_dict_form_too():
    """The dict form is equally valid Ansible, and deploy.yml uses only the string form today.

    Without this, converting a call site to `include_tasks: {file: ...}` would drop it from the
    guard while the guard stayed green on the remaining imports.
    """
    string_form = yaml.safe_load(
        "- ansible.builtin.include_tasks: tasks/k8s_batch.yml\n"
    )
    dict_form = yaml.safe_load(
        "- ansible.builtin.include_tasks:\n    file: tasks/k8s_batch.yml\n"
    )
    assert _task_imports(string_form) == {"ansible/tasks/k8s_batch.yml"}
    assert _task_imports(dict_form) == {"ansible/tasks/k8s_batch.yml"}

    with pytest.raises(AssertionError, match="cannot read the task file"):
        _task_imports(
            yaml.safe_load("- ansible.builtin.include_tasks:\n    apply: {}\n")
        )


def test_every_task_file_deploy_yml_imports_is_visible_to_the_classifier():
    """A file deploy.yml imports must not classify as an EMPTY ChangeSet.

    `ansible/deploy.yml` was broad, but its sibling task dirs matched nothing: every _ACTIVE_*
    regex is anchored to `ansible/roles/`. main() has no catch-all — `if not cs.services:`
    ff-merges unconditionally and the alert helpers no-op on empty fields — so an
    empty-because-unclassified ChangeSet was indistinguishable from an empty-because-docs one:
    silent ff-merge, no alert, no deploy, on files that change what EVERY deploy does.

    Derived from the playbook rather than pinned to today's three paths, so a newly-imported task
    file cannot fall through the classifier the same way.
    """
    imports = _deploy_yml_task_imports()
    assert imports, "parsed no import_tasks/include_tasks out of ansible/deploy.yml"

    invisible = sorted(
        p for p in imports if services_from_changed_paths([p]) == ChangeSet()
    )
    assert not invisible, (
        "deploy.yml imports these task files but the classifier returns an empty ChangeSet for "
        "them, so a push touching one silently ff-merges with no alert and no deploy: %s"
        % invisible
    )


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


def test_declared_denylist_collects_roles_declaring_false():
    sources = {
        "sonarr": 'k8s_autodeploy: false\nk8s_autodeploy_reason: "x"\n',
        "homepage": 'k8s_autodeploy: true\nk8s_autodeploy_reason: "y"\n',
    }
    assert declared_denylist(sources) == frozenset({"sonarr"})


def test_declared_denylist_ignores_the_shared_roles():
    sources = {name: None for name in SHARED_K8S_ROLES}
    sources["sonarr"] = "k8s_autodeploy: false\n"
    assert declared_denylist(sources) == frozenset({"sonarr"})


def test_an_unparseable_declaration_counts_as_denied():
    """Fail closed: a role we cannot read must not silently match the config's view."""
    sources = {"weird": "k8s_autodeploy: maybe\n", "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"weird"})


def test_a_missing_declaration_counts_as_denied():
    sources = {"silent": "some_other_var: 1\n", "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"silent"})


def test_a_missing_defaults_file_counts_as_denied():
    sources = {"gone": None, "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"gone"})


def test_a_trailing_comment_does_not_break_parsing():
    sources = {"r": "k8s_autodeploy: false  # noqa var-naming[no-role-prefix]\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_yaml_no_and_off_read_as_false():
    """PyYAML resolves these to False, so the filter denies them; match that."""
    assert declared_denylist({"a": "k8s_autodeploy: no\n"}) == frozenset({"a"})
    assert declared_denylist({"b": "k8s_autodeploy: off\n"}) == frozenset({"b"})


def test_an_indented_key_is_not_a_declaration():
    """Only a top-level key declares. An indented one belongs to some other block."""
    sources = {"r": "something:\n  k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset(
        {"r"}
    )  # denied: no top-level declaration


def test_a_duplicate_key_with_the_denial_first_reads_as_denied():
    sources = {"r": "k8s_autodeploy: false\nk8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_a_duplicate_key_with_the_denial_last_reads_as_denied():
    # YAML itself is last-key-wins, so a real parser would call this permitted. This reader
    # requires unanimity instead, which is strictly more conservative — see declared_denylist's
    # docstring for why that's the safe direction to diverge in.
    sources = {"r": "k8s_autodeploy: true\nk8s_autodeploy: false\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_no_space_after_the_colon_is_not_a_declaration():
    sources = {"r": "k8s_autodeploy:true\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_a_decoy_declaration_inside_a_quoted_scalar_reads_as_denied():
    # A multi-line quoted YAML scalar can contain a line that starts at column 0 and looks
    # exactly like a top-level `k8s_autodeploy: true` declaration, even though it's just text
    # inside some other key's value. The regex can't tell the difference — it isn't a YAML
    # parser — so it must not let that decoy line outvote the real, later `false`.
    sources = {
        "r": (
            'k8s_autodeploy_reason: "line one\n'
            "k8s_autodeploy: true\n"
            'still inside the quote"\n'
            "k8s_autodeploy: false\n"
        )
    }
    assert declared_denylist(sources) == frozenset({"r"})


def test_crlf_line_endings_are_not_recognized_and_so_deny():
    sources = {"r": "k8s_autodeploy: true\r\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_declares_snapshot_claims_true_for_a_single_claim():
    assert declares_snapshot_claims(
        "k8s_autodeploy_snapshot_pvcs: [bazarr-config]  # noqa\n"
    )


def test_declares_snapshot_claims_true_for_a_two_claim_role():
    assert declares_snapshot_claims(
        "k8s_autodeploy_snapshot_pvcs: [tdarr-configs, tdarr-server]  # noqa\n"
    )


def test_declares_snapshot_claims_false_for_an_empty_list():
    assert not declares_snapshot_claims("k8s_autodeploy_snapshot_pvcs: []\n")


def test_declares_snapshot_claims_false_for_an_absent_key():
    assert not declares_snapshot_claims("some_other_var: 1\n")


def test_declares_snapshot_claims_false_for_none():
    assert not declares_snapshot_claims(None)


def test_declares_snapshot_claims_false_for_empty_string():
    assert not declares_snapshot_claims("")


def test_declares_snapshot_claims_ignores_an_indented_key():
    assert not declares_snapshot_claims(
        "something:\n  k8s_autodeploy_snapshot_pvcs: [x]\n"
    )


# declares_snapshot_claims() is a regex over source text. Since 2026-08-22 it is LOAD-BEARING,
# not just cosmetic: split_k8s_auto_deploy() uses it to decide which promotions count against
# gitops_deploy_k8s_autodeploy_max_claim_services_per_tick. That inverts the safe direction —
# for alert wording a False on unparseable input under-claims (harmless), but for gating it
# reads as claim-free and lets the service batch, which is the exact overrun the cap prevents.
# This test is what holds that closed, so treat a failure here as a deploy-safety failure.
#
# roles/k8s/manifests decides the REAL revert from `yaml.safe_load`'d defaults —
# a different reader of the same file. All 13 roles that declare
# `k8s_autodeploy_snapshot_pvcs` today write it as a single-line list literal, so nothing has
# ever exercised the gap: reformat one to block style and the regex returns False (no revert
# applies, says the alert) while the volume still reverts for real. This walks every role's
# actual defaults/main.yml and pins that the two readers agree on all of them, so a future
# reformat fails this test instead of surfacing as an incident alert that names the wrong thing.
_K8S_ROLES_DIR = pathlib.Path(__file__).parents[3] / "k8s"


def _yaml_declares_claims(text: str) -> bool:
    data = yaml.safe_load(text) or {}
    return bool(data.get("k8s_autodeploy_snapshot_pvcs"))


def test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role():
    mismatches = []
    for defaults_path in sorted(_K8S_ROLES_DIR.glob("*/defaults/main.yml")):
        text = defaults_path.read_text()
        regex_verdict = declares_snapshot_claims(text)
        yaml_verdict = _yaml_declares_claims(text)
        if regex_verdict != yaml_verdict:
            mismatches.append(
                f"{defaults_path.relative_to(_K8S_ROLES_DIR.parent)}: "
                f"regex={regex_verdict} yaml={yaml_verdict}"
            )
    assert not mismatches, (
        "declares_snapshot_claims()'s regex disagrees with what roles/k8s/manifests actually "
        "reads via yaml.safe_load for:\n" + "\n".join(mismatches)
    )


def test_rollback_volume_revert_note_reports_the_redeploy_failure_when_it_failed():
    """The redeploy raising means the revert task inside roles/k8s/manifests may never have
    run — the note must say so, not claim a revert was attempted."""
    note = rollback_volume_revert_note({"sonarr"}, frozenset(), "boom")
    assert "rollback redeploy itself failed" in note
    assert "boom" in note
    assert "Volume revert" not in note


def test_rollback_volume_revert_note_says_no_claims_when_none_declare():
    note = rollback_volume_revert_note({"speedtest"}, frozenset(), None)
    assert "No service" in note
    assert "no volume revert applies" in note


def test_rollback_volume_revert_note_names_only_the_services_that_revert():
    note = rollback_volume_revert_note(
        {"sonarr", "speedtest"}, frozenset({"sonarr"}), None
    )
    assert "`sonarr`" in note
    assert "speedtest" in note  # named as unaffected, not silently dropped
    assert "declares no `k8s_autodeploy_snapshot_pvcs` and is unaffected" in note


def test_rollback_volume_revert_note_omits_the_unaffected_aside_when_all_revert():
    note = rollback_volume_revert_note({"sonarr"}, frozenset({"sonarr"}), None)
    assert "unaffected" not in note


def test_k8s_role_paths_finds_a_normal_role():
    listing = "ansible/roles/k8s/sonarr/defaults/main.yml\nansible/roles/k8s/sonarr/tasks/main.yml\n"
    assert k8s_role_paths(listing) == {
        "sonarr": "ansible/roles/k8s/sonarr/defaults/main.yml"
    }


def test_k8s_role_paths_a_role_with_no_defaults_maps_to_none():
    listing = "ansible/roles/k8s/homepage/tasks/main.yml\n"
    assert k8s_role_paths(listing) == {"homepage": None}


def test_k8s_role_paths_a_defaults_dir_holding_something_other_than_main_yml():
    listing = "ansible/roles/k8s/sonarr/defaults/other.yml\n"
    assert k8s_role_paths(listing) == {"sonarr": None}


def test_k8s_role_paths_ignores_a_stray_file_directly_under_roles_k8s():
    listing = "ansible/roles/k8s/README.md\n"
    assert k8s_role_paths(listing) == {}


def test_k8s_role_paths_empty_listing():
    assert k8s_role_paths("") == {}


def test_k8s_role_paths_order_does_not_matter():
    before_after = (
        "ansible/roles/k8s/sonarr/tasks/main.yml\n"
        "ansible/roles/k8s/sonarr/defaults/main.yml\n"
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2\n"
    )
    after_before = (
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2\n"
        "ansible/roles/k8s/sonarr/defaults/main.yml\n"
        "ansible/roles/k8s/sonarr/tasks/main.yml\n"
    )
    expected = {"sonarr": "ansible/roles/k8s/sonarr/defaults/main.yml"}
    assert k8s_role_paths(before_after) == expected
    assert k8s_role_paths(after_before) == expected


# ── deploy_k8s ────────────────────────────────────────────────────────────────────────────────
# gitops_deploy.py reads /etc/gitops-deploy/config.env at import time (`C = cfg()`), which
# doesn't exist in CI — see test_gitops_discord_contract.py's docstring, which is why every other
# guard on that module is an AST source check rather than an import. Stub host_lib.parse_env_file
# with canned values BEFORE the only import of gitops_deploy in this file, so the import behaves
# identically in CI and on a host where the real config.env exists, and this suite never reads
# the real secrets file (forbidden — it's SOPS-managed, see the role CLAUDE.md).
def _import_gitops_deploy():
    import host_lib

    real_parse_env_file = host_lib.parse_env_file
    host_lib.parse_env_file = lambda _path: {
        "REPO_DIR": "/tmp/gitops-test-repo",
        "HOSTNAME": "test-host",
        "DISCORD_WEBHOOK": "https://discord.example/webhook",
    }
    try:
        sys.modules.pop("gitops_deploy", None)
        import gitops_deploy
    finally:
        host_lib.parse_env_file = real_parse_env_file
    return gitops_deploy


gitops_deploy = _import_gitops_deploy()


def _capture_run(monkeypatch):
    """Patch gitops_deploy.run() to record every call instead of shelling out, and return the
    list it appends to."""

    class _Call:
        def __init__(self, argv, kwargs):
            self.argv = argv
            self.kwargs = kwargs

    calls: list[_Call] = []

    def _fake_run(argv, **kwargs):
        calls.append(_Call(argv, kwargs))
        return ""

    monkeypatch.setattr(gitops_deploy, "run", _fake_run)
    return calls


_FORWARD_ARGV = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/deploy.yml",
    "--tags",
    "sonarr",
]


def test_deploy_k8s_passes_no_extra_vars_by_default(monkeypatch) -> None:
    """The ordinary deploy must be byte-identical to what it was before this slice. ~50
    services go through this call on every tick. Pins the full argv, not just -e's absence —
    a stray extra arg anywhere else in the list would pass a presence-only check."""
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0)
    assert calls[0].argv == _FORWARD_ARGV


def test_deploy_k8s_passes_the_restore_sha_when_given(monkeypatch) -> None:
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="deadbeef")
    assert calls[0].argv == _FORWARD_ARGV + ["-e", "k8s_restore_snapshot_sha=deadbeef"]


def test_deploy_k8s_treats_a_whitespace_only_restore_sha_as_absent(monkeypatch) -> None:
    """restore_sha="" or all-whitespace must stay inert, matching the manifests role's own
    `| trim | length > 0` guard — a blank-but-truthy string must not add a broken `-e` arg."""
    calls = _capture_run(monkeypatch)
    gitops_deploy.deploy_k8s({"sonarr"}, 900.0, restore_sha="   ")
    assert calls[0].argv == _FORWARD_ARGV


# ── the rollback call site in main() ─────────────────────────────────────────────────────────
# main() shells out to git, queries GitHub for a CI verdict over HTTP, posts to Discord, and
# touches several state files under /var/lib/gitops-deploy — exercising it end-to-end would mean
# mocking all of that for one two-line assertion. This parses the ACTUAL call arguments Python
# executes (not comment text), the same AST-source-guard shape test_gitops_discord_contract.py
# already uses for the rest of this un-importable-in-CI module.
_GITOPS_SRC = pathlib.Path(__file__).with_name("gitops_deploy.py")


def _deploy_k8s_calls_in_main() -> list[ast.Call]:
    tree = ast.parse(_GITOPS_SRC.read_text())
    main_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    return [
        n
        for n in ast.walk(main_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "deploy_k8s"
    ]


def test_the_rollback_redeploy_passes_the_FAILED_sha_not_the_good_one():
    """The snapshot worth reverting to was taken before the failed deploy, so it is named for
    `origin` — the commit being rolled back FROM. Passing `local` here would look correct, find
    no snapshot for a first-time rollback, and fail the deploy; worse, on a second rollback of
    the same service it would find a stale snapshot and revert to the wrong point."""
    calls = _deploy_k8s_calls_in_main()
    assert len(calls) == 2, (
        "expected exactly one forward deploy_k8s call and one rollback redeploy in main()"
    )
    forward, rollback = calls

    forward_kwargs = {kw.arg for kw in forward.keywords}
    assert "restore_sha" not in forward_kwargs, (
        "the forward deploy must not pass restore_sha"
    )

    rollback_kwargs = {kw.arg: kw.value for kw in rollback.keywords}
    assert "restore_sha" in rollback_kwargs, (
        "the rollback redeploy must pass restore_sha"
    )
    # Pin the exact expression, not a prefix: `startswith("origin")` alone is satisfied by the
    # full 40-char `origin` (volume-snapshot names with `git rev-parse --short=8`, so a 40-char
    # SHA matches no snapshot and the revert silently never runs) and by an unrelated
    # `origin_decoy` variable — neither is what this call site must send.
    sha_expr = ast.unparse(rollback_kwargs["restore_sha"])
    assert sha_expr == "origin[:8]", (
        f"rollback restore_sha must be exactly `origin[:8]` — the FAILED commit's short SHA, "
        f"matching how volume-snapshot names its snapshots — got `{sha_expr}`"
    )


def test_the_rollback_redeploy_uses_its_own_timeout_budget():
    """Task 4's addendum: give the rollback redeploy a distinct budget rather than sharing
    K8S_DEPLOY_TIMEOUT_S, since it does strictly more work than the forward deploy."""
    forward, rollback = _deploy_k8s_calls_in_main()
    assert ast.unparse(forward.args[1]) == "K8S_DEPLOY_TIMEOUT_S"
    assert ast.unparse(rollback.args[1]) == "K8S_ROLLBACK_TIMEOUT_S"


# ── run()'s timeout must kill the whole process group ───────────────────────────────────────────
# `uv run ansible-playbook ...` is a GRANDCHILD of run()'s subprocess (uv forks it rather than
# exec'ing into it). `subprocess.run(timeout=)` DOES return promptly on timeout — its internal
# communicate() raises on the wall-clock deadline, not on pipe EOF — but it kills only the DIRECT
# child (uv). Verified empirically against the pre-fix implementation: the call returns on time
# and the grandchild is still alive at that moment, left running as an orphan with nothing
# watching it. That is how K8S_ROLLBACK_TIMEOUT_S stopped being an actual bound on the underlying
# ansible-playbook: gitops_deploy.py moves on while the timed-out run keeps mutating the cluster,
# and the real stop becomes systemd's TimeoutStartSec SIGTERM against the wrapping unit, which can
# land mid-rollback. This shape reproduces it directly: a shell script backgrounds a grandchild
# that outlives a naive kill-the-direct-child-only fix, so the test fails against the OLD run()
# and passes only once the whole process group is killed.
_GRANDCHILD_SHAPE = """#!/bin/sh
sh -c 'echo $$ > "{pidfile}"; sleep 30' &
wait
"""


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_timeout_kills_the_whole_process_group(tmp_path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.sh"
    script.write_text(_GRANDCHILD_SHAPE.format(pidfile=pidfile))
    script.chmod(0o755)

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        gitops_deploy.run(["sh", str(script)], cwd=str(tmp_path), timeout=1.0)
    elapsed = time.monotonic() - start

    # Both the buggy and the fixed run() return around the 1.0s deadline — the deadline is a
    # wall-clock check inside communicate(), not a wait for pipe EOF, so this alone does not
    # discriminate them. It is here as a sanity bound; the real regression check is the
    # grandchild-liveness assert below.
    assert elapsed < 10, (
        f"run() took {elapsed:.1f}s to return after a 1.0s timeout — expected it to return "
        f"around the deadline regardless of whether the fix is applied"
    )

    deadline = time.monotonic() + 2
    grandchild_pid = None
    while grandchild_pid is None and time.monotonic() < deadline:
        if pidfile.exists():
            grandchild_pid = int(pidfile.read_text().strip())
        else:
            time.sleep(0.05)
    assert grandchild_pid is not None, "the grandchild never started"

    # SIGKILL is instant but reaping is not: once its own parent (the script) is also killed,
    # the grandchild is reparented and reaped by the nearest subreaper — poll briefly instead
    # of asserting the instant killpg returns.
    deadline = time.monotonic() + 3
    while _pid_is_alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_alive(grandchild_pid), (
        f"grandchild pid {grandchild_pid} outlived the timeout — only the direct child was "
        f"killed, not its process group"
    )


# ── the claim-declaring cap (2026-08-22 review H2) ──────────────────────────────────────────
# Each claim-declaring service pays its own snapshot+revert phase SERIALLY inside the single
# rollback playbook run, while K8S_ROLLBACK_TIMEOUT_S is derived for the worst SINGLE one — so
# two co-batched already exceed it and killpg lands mid-revert, after volume-revert has scaled
# the workload to zero and attached its volume in maintenance mode. The budget arithmetic itself
# is pinned by ansible/tests/test_rollback_timeout_budget.py; these cover the partition.


def test_split_k8s_caps_claim_declaring_services_separately():
    paths = [_defaults_for(s) for s in ("radarr", "sonarr", "bazarr")]
    cs = _split(
        paths,
        claim_services=("radarr", "sonarr", "bazarr"),
        max_claim_services_per_tick=1,
    )
    assert len(cs.k8s_deploy) == 1
    assert cs.k8s == {"radarr", "sonarr", "bazarr"} - cs.k8s_deploy


def test_split_k8s_claim_cap_still_batches_claim_free_services():
    # Why a SEPARATE cap rather than max_per_tick=1: claim-free services cost nothing on the
    # revert path, so they must keep batching.
    paths = [_defaults_for(s) for s in ("radarr", "sonarr", "speedtest", "littlelink")]
    cs = _split(
        paths,
        claim_services=("radarr", "sonarr"),
        max_claim_services_per_tick=1,
        max_per_tick=3,
    )
    assert len([s for s in cs.k8s_deploy if s in ("radarr", "sonarr")]) == 1
    assert {"speedtest", "littlelink"} <= cs.k8s_deploy


def test_split_k8s_claim_cap_respects_max_per_tick_too():
    # Both caps bind; the claim cap must not become a way to exceed the batch cap.
    paths = [
        _defaults_for(s) for s in ("radarr", "speedtest", "littlelink", "freshrss")
    ]
    cs = _split(
        paths,
        claim_services=("radarr",),
        max_claim_services_per_tick=1,
        max_per_tick=2,
    )
    assert len(cs.k8s_deploy) == 2


def test_split_k8s_claim_cap_is_deterministic():
    paths = [_defaults_for(s) for s in ("radarr", "sonarr", "bazarr")]
    claims = ("radarr", "sonarr", "bazarr")
    first = _split(
        paths, claim_services=claims, max_claim_services_per_tick=1
    ).k8s_deploy
    second = _split(
        paths, claim_services=claims, max_claim_services_per_tick=1
    ).k8s_deploy
    assert first == second


def test_split_k8s_defers_the_surplus_when_every_promotable_declares_claims():
    # No claim-free service to fill the batch: promote exactly one, and the rest stay in cs.k8s,
    # which defer-and-alerts.
    paths = [_defaults_for(s) for s in ("radarr", "sonarr")]
    cs = _split(
        paths, claim_services=("radarr", "sonarr"), max_claim_services_per_tick=1
    )
    assert len(cs.k8s_deploy) == 1
    assert len(cs.k8s) == 1


def test_split_k8s_claim_cap_of_zero_leaves_the_old_behaviour():
    # 0 disables the claim cap; max_per_tick alone then governs, exactly as before this landed.
    paths = [_defaults_for(s) for s in ("radarr", "sonarr")]
    cs = _split(
        paths, claim_services=("radarr", "sonarr"), max_claim_services_per_tick=0
    )
    assert cs.k8s_deploy == {"radarr", "sonarr"}


def _prescribed_tags(msg: str) -> set[str]:
    """Every tag the remediation message actually tells an operator to pass to --tags."""
    out: set[str] = set()
    for chunk in msg.split("--tags ")[1:]:
        out.update(t for t in chunk.split("`")[0].strip().split(",") if t)
    return out


def test_k8s_remediation_never_prescribes_a_tag_that_deploys_nothing():
    """The alert must not name `--tags <role>` for a role with no containers_list entry.

    deploy.yml includes k8s roles per containers_list entry with tags: [<entry name>], so a tag
    matching no entry selects nothing and Ansible EXITS 0 — the operator runs the prescribed
    command, sees green, and the change is never applied. Eight roles are in that position and
    they are the shared plane (manifests is the apply+rollout path for every workload;
    volume-revert is the auto-deploy rollback path).

    Cross-checked against scripts/deploy_tools/deploy_tags.known_tags(), the same source ./scripts/deploy.sh
    validates against, so the alert and the wrapper cannot drift apart.
    """
    import sys

    sys.path.insert(
        0, str(pathlib.Path(__file__).resolve().parents[5] / "scripts" / "deploy_tools")
    )
    import deploy_tags

    declared = deploy_tags.known_tags()
    roles = {p.name for p in _K8S_ROLES_DIR.iterdir() if p.is_dir()}
    shared = roles - declared
    assert shared, (
        "expected some roles/k8s/ dirs to have no deploy tag; if this is now empty the "
        "remediation split is dead code and can be removed"
    )

    # An all-shared set must prescribe a full deploy and prescribe no tags at all. Asserting on
    # PRESCRIBED tags rather than the literal string "--tags", which also appears in the message's
    # own explanation of why a tag-scoped redeploy would not work.
    msg = k8s_remediation(shared, declared)
    assert _prescribed_tags(msg) == set(), (
        "k8s_remediation prescribed a --tags redeploy for roles with no containers_list "
        "entry: %s" % sorted(shared)
    )
    assert "`ansible-playbook ansible/deploy.yml`" in msg

    # A declared role still gets the cheap scoped form.
    one = sorted(roles & declared)[:1]
    if one:
        scoped = k8s_remediation(set(one), declared)
        assert "--tags %s" % one[0] in scoped

    # Every tag the mixed form prescribes must itself be deployable, and the shared roles must
    # still get the full-deploy instruction alongside.
    mixed = k8s_remediation(shared | set(one), declared)
    assert _prescribed_tags(mixed) <= declared, (
        "the mixed form prescribed undeployable tags: %s"
        % sorted(_prescribed_tags(mixed) - declared)
    )
    assert "`ansible-playbook ansible/deploy.yml`" in mixed


def test_a_shared_module_edit_names_every_consumer_role():
    """`_ACTIVE_K8S` maps a path to the role whose directory holds it, which is right for a
    manifest and wrong for a shared library. bridge_common.py lives under monitor-bridge and
    autofix-bridge imports it, so after the #407 split an edit there emitted
    `--tags monitor-bridge` alone and autofix-bridge's ConfigMap kept the old copy with
    nothing reporting it (2026-08-25 review M-2).
    """
    repo = pathlib.Path(__file__).resolve().parents[5]
    paths = ["ansible/roles/k8s/monitor-bridge/files/bridge_common.py"]
    consumers = shared_module_consumers(paths, repo)
    assert "autofix-bridge" in consumers, (
        "the deployer cannot see that autofix-bridge imports bridge_common, so a shared "
        "edit ff-merges leaving its ConfigMap stale: %s" % sorted(consumers)
    )
    assert "monitor-bridge" not in consumers, "the owning role is already in cs.k8s"

    declared = {"monitor-bridge", "autofix-bridge"}
    assert (
        _prescribed_tags(k8s_remediation({"monitor-bridge"}, declared, consumers))
        == declared
    )


def test_a_consumer_this_host_does_not_declare_is_not_escalated():
    """Intersect with `declared` BEFORE the union. A consumer absent from this host's
    containers_list has no deploy tag here, so folding it in raw would land it in `shared`
    and escalate a scoped `--tags` into "run a full deploy" -- for a role this host does not
    deploy at all.
    """
    declared = {"monitor-bridge"}
    msg = k8s_remediation({"monitor-bridge"}, declared, {"autofix-bridge"})
    assert _prescribed_tags(msg) == {"monitor-bridge"}
    assert "`ansible-playbook ansible/deploy.yml`" not in msg, (
        "an undeclared consumer escalated the instruction to a full deploy: %s" % msg
    )
