"""authelia's OIDC signing key lives in Secret authelia-oidc-jwk, behind authelia_k8s_manage_oidc.

The key is a multi-line PEM block, so no Flux placeholder can carry it, and target slice R2's
PR 7 moved it out of authelia-config. Three things have to agree for the portal to start:

- authelia passes explicit file lists, and a passed list wins whole, so a template the
  `manifests_secret_files` expression does not name is never rendered. The Deployment would
  then mount a Secret no deploy wrote. The lists are evaluated through Ansible's templar,
  because `lib.k8s_roles.resolved_manifest_files` counts every basename in a conditional
  branch and would pass with the lever's arm broken.
- The Deployment's volume, mount and `X_AUTHELIA_CONFIG_FILTERS` follow the same lever.
- With the filter on, Authelia runs Go templating over the whole configuration.yml, so a real
  value holding `{{` would stop it starting. Two tasks count the actions in the real render
  and fail the deploy unless there is one. Their count expression is evaluated here over a
  fixture in place of the template lookup.
"""

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from _helpers import ROLES, load_defaults, load_tasks
from _k8s_render import render_role_template, rendered_texts

ROLE_DIR = ROLES / "k8s" / "authelia"
TASKS = ROLE_DIR / "tasks" / "main.yml"
LEVER = "authelia_k8s_manage_oidc"
JWK_SECRET = "oidc-jwk-secret.yaml"
COUNT_TASK = "Count the template actions in Authelia's rendered configuration"
ASSERT_TASK = (
    "Fail unless the jwks key expression is the configuration's only template action"
)
INCLUDE_TASK = "Deploy Authelia to the cluster"


def _tasks() -> dict[str, tuple[int, dict]]:
    return {t["name"]: (i, t) for i, t in enumerate(load_tasks(TASKS)) if "name" in t}


def _lists(lever: bool) -> tuple[list[str], list[str]]:
    """`(manifests_files, manifests_secret_files)` as the include passes them, at `lever`."""
    _i, include = _tasks()[INCLUDE_TASK]
    defaults = load_defaults(ROLE_DIR)
    variables = {
        name: defaults[name]
        for name in (
            "authelia_k8s_manage_crowdsec",
            "authelia_k8s_redis_sessions",
        )
    } | {LEVER: lever}
    templar = Templar(loader=DataLoader(), variables=variables)
    files, secret = (
        list(templar.template(trust_as_template(include["vars"][key])))
        for key in ("manifests_files", "manifests_secret_files")
    )
    return files, secret


def test_the_lever_on_lists_the_jwk_secret_as_a_secret_file():
    files, secret = _lists(True)
    assert JWK_SECRET in secret
    assert JWK_SECRET not in files
    assert "config-secret.yaml" in secret


def test_the_lever_off_lists_the_jwk_secret_nowhere():
    files, secret = _lists(False)
    assert JWK_SECRET not in secret + files
    assert "config-secret.yaml" in secret


# `-e authelia_k8s_manage_oidc=false` arrives as a string, which the list reads through `| bool`.
@pytest.mark.parametrize("off_value", [False, "false"])
def test_the_deployment_mounts_the_jwk_secret_only_with_the_lever_on(off_value):
    on = render_role_template("authelia", "deployment.yaml.j2")
    off = render_role_template("authelia", "deployment.yaml.j2", {LEVER: off_value})
    for marker in ("secretName: authelia-oidc-jwk", "X_AUTHELIA_CONFIG_FILTERS"):
        assert marker in on
        assert marker not in off


# --- the count assert ---------------------------------------------------------------------


def _config_secret_text() -> str:
    (text,) = [
        text
        for role, template, text in rendered_texts()
        if role == "authelia" and template == "config-secret.yaml.j2"
    ]
    return text


def _assert_passes(config_secret: str, lever: bool = True) -> bool | None:
    """Whether the real assert holds over `config_secret`; None when the tasks are skipped."""
    tasks = _tasks()
    _i, count = tasks[COUNT_TASK]
    _i, check = tasks[ASSERT_TASK]
    variables = {LEVER: lever}
    templar = Templar(loader=DataLoader(), variables=variables)
    if not templar.template(trust_as_template("{{ " + count["when"] + " }}")):
        assert not templar.template(trust_as_template("{{ " + check["when"] + " }}"))
        return None
    variables["authelia_k8s_rendered_config_secret"] = config_secret
    templar = Templar(loader=DataLoader(), variables=variables)
    fact = count["ansible.builtin.set_fact"]["authelia_k8s_config_action_count"]
    variables["authelia_k8s_config_action_count"] = templar.template(
        trust_as_template(fact)
    )
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.template(trust_as_template("{{ " + clause + " }}"))
        for clause in check["ansible.builtin.assert"]["that"]
    )


def test_the_rendered_config_with_only_the_filter_expression_passes():
    assert _assert_passes(_config_secret_text()) is True


def test_a_value_holding_a_template_action_fails():
    text = _config_secret_text()
    assert "hmac_secret: '" in text
    assert (
        _assert_passes(text.replace("hmac_secret: '", "hmac_secret: 'x{{y", 1)) is False
    )


def test_a_config_with_no_filter_expression_fails():
    """Non-vacuity: the count must find the expression, not pass on zero."""
    text = _config_secret_text()
    without = "\n".join(ln for ln in text.splitlines() if "{{- secret" not in ln)
    assert _assert_passes(without) is False


def test_the_lever_off_skips_both_tasks():
    assert _assert_passes(_config_secret_text(), lever=False) is None


def test_both_tasks_precede_the_include_and_hide_only_the_render():
    tasks = _tasks()
    (count_at, count), (assert_at, check) = tasks[COUNT_TASK], tasks[ASSERT_TASK]
    include_at, _include = tasks[INCLUDE_TASK]
    assert count_at < assert_at < include_at
    assert count.get("no_log") is True
    assert "no_log" not in check
    assert any("== 1" in clause for clause in check["ansible.builtin.assert"]["that"])
    assert count["when"] == check["when"] == f"{LEVER} | bool"
