#!/usr/bin/env python3
"""Tests for the compose-template validator's $$-escaping check.

Docker Compose interpolates `$VAR` / `${VAR}` / `$(...)` in string values at parse
time, so a shell `$` meant for the container must be written `$$` in the template.
`recreate: auto` and `ansible-lint` both miss this; a lone `$` either gets blanked
(missing env) or interpolated, silently breaking a healthcheck/command. The check
is context-aware: it only inspects command/entrypoint/healthcheck.test, so the
intentional `${GID-...}` interpolation some services use in `environment:` is not
flagged.

Run: uv run pytest scripts/test_validate_compose_templates.py
"""

import importlib.util
import os

_MOD = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "validate_compose_templates.py"
)
_spec = importlib.util.spec_from_file_location("validate_compose_templates", _MOD)
vct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vct)


def _docs(spec):
    """One rendered compose doc with a single service named 'svc'."""
    return [{"services": {"svc": spec}}]


# --- clean: $ correctly doubled, or no relevant key --------------------------


def test_doubled_dollar_in_healthcheck_is_clean():
    docs = _docs(
        {"healthcheck": {"test": ["CMD-SHELL", 'x=$$(date) && [ "$${x:-0}" ]']}}
    )
    assert vct.find_dollar_escape_bugs(docs) == []


def test_doubled_dollar_in_command_list_is_clean():
    # prometheus-style node-exporter arg with an escaped regex anchor
    docs = _docs(
        {"command": ["--collector.filesystem.mount-points-exclude=^/(sys|proc)($$|/)"]}
    )
    assert vct.find_dollar_escape_bugs(docs) == []


def test_no_command_or_healthcheck_is_clean():
    assert vct.find_dollar_escape_bugs(_docs({"image": "nginx"})) == []


def test_environment_interpolation_is_not_flagged():
    # the deliberate Compose ${GID-...} interpolation (crowdsec/traefik) lives in
    # environment:, which the check intentionally does NOT inspect.
    docs = _docs({"environment": {"GID": "${GID-1000}"}, "command": "run --port 8080"})
    assert vct.find_dollar_escape_bugs(docs) == []


# --- buggy: a lone (un-doubled) $ in a shell context -------------------------


def test_lone_dollar_in_healthcheck_is_flagged():
    docs = _docs(
        {"healthcheck": {"test": ["CMD-SHELL", "curl http://$HOSTNAME/ || exit 1"]}}
    )
    bugs = vct.find_dollar_escape_bugs(docs)
    assert len(bugs) == 1
    svc, key, snippet = bugs[0]
    assert svc == "svc" and key == "healthcheck.test" and "$HOSTNAME" in snippet


def test_lone_dollar_in_command_string_is_flagged():
    docs = _docs({"command": "sh -c 'echo $(hostname)'"})
    bugs = vct.find_dollar_escape_bugs(docs)
    assert len(bugs) == 1 and bugs[0][1] == "command"


def test_lone_dollar_in_entrypoint_list_is_flagged():
    docs = _docs({"entrypoint": ["sh", "-c", "exec $APP"]})
    bugs = vct.find_dollar_escape_bugs(docs)
    assert len(bugs) == 1 and bugs[0][1] == "entrypoint"


def test_triple_dollar_still_flags_the_interpolated_remainder():
    # $$$ = one escaped $$ plus a lone ${x} -> still a (likely) bug.
    docs = _docs({"command": "echo $$${x}"})
    assert len(vct.find_dollar_escape_bugs(docs)) == 1


# --- structure walking / robustness -----------------------------------------


def test_walks_all_services_and_attributes_the_right_one():
    docs = [{"services": {"a": {"command": "ok"}, "b": {"command": "echo $X"}}}]
    bugs = vct.find_dollar_escape_bugs(docs)
    assert [s for s, _, _ in bugs] == ["b"]


def test_tolerates_non_service_docs():
    assert (
        vct.find_dollar_escape_bugs([None, {"version": "3"}, "junk", {"services": "x"}])
        == []
    )


# --- watchtower label `=` guard ----------------------------------------------
# Docker splits a LIST-form label on the first `=` only. A `:`-separated watchtower
# label (e.g. `...depends-on:docker-proxy`) parses as a key with an EMPTY value, so the
# directive silently no-ops. Renders cleanly + passes YAML lint, so nothing else caught it.


def test_watchtower_labels_with_equals_are_clean():
    docs = _docs(
        {
            "labels": [
                "com.centurylinklabs.watchtower.enable=false",
                "com.centurylinklabs.watchtower.depends-on=docker-proxy",
            ]
        }
    )
    assert vct.find_watchtower_label_bugs(docs) == []


def test_watchtower_label_with_colon_is_flagged():
    docs = _docs({"labels": ["com.centurylinklabs.watchtower.depends-on:docker-proxy"]})
    assert vct.find_watchtower_label_bugs(docs) == [
        ("svc", "com.centurylinklabs.watchtower.depends-on:docker-proxy")
    ]


def test_watchtower_label_with_space_colon_is_flagged():
    # the grafana ': prometheus' variant
    docs = _docs({"labels": ["com.centurylinklabs.watchtower.depends-on: prometheus"]})
    assert len(vct.find_watchtower_label_bugs(docs)) == 1


def test_dict_form_watchtower_labels_not_flagged():
    # mapping-form labels are inherently key:value — no `=` needed
    docs = _docs({"labels": {"com.centurylinklabs.watchtower.enable": "false"}})
    assert vct.find_watchtower_label_bugs(docs) == []


def test_non_watchtower_label_without_equals_is_ignored():
    docs = _docs({"labels": ["some.other.label:value"]})
    assert vct.find_watchtower_label_bugs(docs) == []


# --- real-render regression guard --------------------------------------------
# The synthetic tests above only exercise the two detector functions. This is the
# ONLY pytest coverage of the *real* render path (every service in both hosts'
# containers_list, through ALL the shared macros: networks/resources/healthcheck/
# traefik/autokuma/expose). It mirrors the sibling validators' live-config guards
# (validate_ha_config.test_validate_real_config_is_clean, validate_grafana
# .test_real_role_passes). Without it, a Jinja-indent regression in a macro that
# the prek `validate-compose-templates` hook's file filter doesn't match (e.g.
# networks.yml.j2) would slip past CI entirely.


def test_real_templates_render_clean():
    assert vct.main() == 0


# --- cap_drop policy guard ---------------------------------------------------
# Every service should drop ALL capabilities (defense in depth), adding back only what it
# proves it needs. New services kept silently drifting out of this (n8n-runners/nut/unbound
# post-dated the hardening sprints), so enforce it; documented exceptions go in CAP_DROP_EXEMPT.


def test_cap_drop_all_is_clean():
    assert vct.find_missing_cap_drop(_docs({"cap_drop": ["ALL"]})) == []


def test_cap_drop_all_lowercase_is_clean():
    assert vct.find_missing_cap_drop(_docs({"cap_drop": ["all"]})) == []


def test_missing_cap_drop_is_flagged():
    assert vct.find_missing_cap_drop(_docs({"image": "nginx"})) == ["svc"]


def test_partial_cap_drop_without_all_is_flagged():
    # dropping a single cap is NOT the policy (drop ALL, add back minimal)
    assert vct.find_missing_cap_drop(_docs({"cap_drop": ["NET_RAW"]})) == ["svc"]


def test_cap_drop_exempt_service_is_skipped():
    assert vct.find_missing_cap_drop(_docs({"image": "x"}), exempt={"svc"}) == []


def test_cap_drop_walks_all_services():
    docs = [{"services": {"a": {"cap_drop": ["ALL"]}, "b": {"image": "x"}}}]
    assert vct.find_missing_cap_drop(docs) == ["b"]


# --- watchtower update-policy guard ------------------------------------------
# Watchtower runs monitor-all, so a mutable-tag service WITHOUT an opt-out is auto-updated.
# That's fine for the disposable pool but silently swept up karakeep/janitorr (stateful,
# coupled). Force an explicit decision: a mutable tag must EITHER opt out (enable=false) OR
# be listed in WATCHTOWER_AUTOUPDATE (intentionally auto-updated). Version-pinned tags are exempt.


def test_pinned_tag_is_clean():
    assert (
        vct.find_undeclared_update_policy(_docs({"image": "pihole/pihole:2026.05.0"}))
        == []
    )


def test_mutable_tag_without_decision_is_flagged():
    assert vct.find_undeclared_update_policy(_docs({"image": "x:latest"})) == ["svc"]


def test_mutable_tag_with_optout_is_clean():
    docs = _docs(
        {"image": "x:latest", "labels": ["com.centurylinklabs.watchtower.enable=false"]}
    )
    assert vct.find_undeclared_update_policy(docs) == []


def test_mutable_tag_with_mapping_optout_is_clean():
    docs = _docs(
        {
            "image": "x:latest",
            "labels": {"com.centurylinklabs.watchtower.enable": "false"},
        }
    )
    assert vct.find_undeclared_update_policy(docs) == []


def test_mutable_tag_on_autoupdate_allowlist_is_clean():
    assert (
        vct.find_undeclared_update_policy(
            _docs({"image": "x:latest"}), autoupdate={"svc"}
        )
        == []
    )


def test_jvm_stable_channel_tag_is_mutable():
    assert vct.find_undeclared_update_policy(
        _docs({"image": "schaka/janitorr:jvm-stable"})
    ) == ["svc"]


def test_channel_prefix_variant_tag_is_mutable():
    # scrutiny ships ghcr.io/analogj/scrutiny:master-web / :master-collector — a rolling
    # `master` branch build with a component suffix. The channel word is the PREFIX here,
    # not a `-stable` suffix, so it must still force an explicit update-policy decision.
    assert vct.find_undeclared_update_policy(
        _docs({"image": "ghcr.io/analogj/scrutiny:master-web"})
    ) == ["svc"]
    assert vct.find_undeclared_update_policy(
        _docs({"image": "ghcr.io/analogj/scrutiny:master-collector"})
    ) == ["svc"]


def test_untagged_image_is_mutable():
    assert vct.find_undeclared_update_policy(_docs({"image": "nginx"})) == ["svc"]


def test_build_only_service_without_image_is_clean():
    # a `build:`-only service (no image: key) has no tag to police
    assert vct.find_undeclared_update_policy(_docs({"build": {"context": "."}})) == []
