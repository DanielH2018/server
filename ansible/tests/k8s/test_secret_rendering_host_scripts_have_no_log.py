"""A k8s-role template task that renders a secret into a host script carries `no_log: true`.

`ansible.builtin.template` puts the rendered body in its result only under diff mode, and
`scripts/deploy.sh` forwards the operator's flags to ansible-playbook unchanged, so a
`--check --diff` rehearsal prints every changed script's before/after to stdout and to
`ansible.log` (`log_path = ./ansible.log`, ansible.cfg). A script that interpolates a Kuma push
token is therefore a credential in the log the moment someone types `--diff`. `no_log: true` is
the one setting that keeps the body out of the callback whatever the flags say, and
`.claude/rules/ansible.md` requires it on any task that handles a secret. Five sibling tasks
carried it; three did not until 2026-09-17 (#1932), and nothing had censused them.

The census: every `template` task under `roles/k8s/*/tasks/` whose `src` is a `*.sh.j2`, whose
body references a name in `ansible/secret_rotation.yml` as a whole word. The registry is
plaintext by design (names, never values), so this runs on a CI runner with no SOPS key. The
name list (`_helpers.registry_secret_names`) is shared with the release_bin guard so the two cannot disagree
about what counts as a secret.

Run: uv run pytest ansible/tests/k8s/test_secret_rendering_host_scripts_have_no_log.py
"""

import re

import pytest
from _helpers import K8S_ROLES, load_tasks, registry_secret_names, walk_tasks

TEMPLATE_MODULES = {"ansible.builtin.template", "template"}

# The three #1932 found without no_log, plus two of the siblings that had it. Named so the
# census cannot go vacuous: a rename of `tasks/main.yml`, of the src, or of the registry
# entry the body references drops the script out of the set silently, and the assertion below
# would pass over nothing.
KNOWN_SECRET_SCRIPTS = frozenset(
    {
        ("traefik", "cloudflare-ip-drift.sh.j2"),
        ("crowdsec", "crowdsec-update-home-allowlist.sh.j2"),
        ("crowdsec", "crowdsec-appsec-verify.sh.j2"),
        ("janitorr", "janitorr-health.sh.j2"),
        ("configarr", "configarr-health.sh.j2"),
        # Invisible to this census until 2026-09-18: its token was referenced nowhere but the
        # script, so no registry name matched the body (#1937).
        ("registry", "registry-gc.sh.j2"),
    }
)


# --- the rule, as a predicate ----------------------------------------------------------


def renders_a_secret(body: str, names) -> list[str]:
    """The registry names `body` references as whole words."""
    return sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", body))


def task_keeps_the_body_out_of_the_log(task: dict) -> bool:
    return task.get("no_log") is True


# --- red proofs -----------------------------------------------------------------------


def test_a_push_url_line_is_flagged():
    body = 'PUSH_URL="https://kuma/api/push/{{ monitor_bridge_appsec_push_token }}"'
    assert renders_a_secret(body, ["monitor_bridge_appsec_push_token"])


def test_a_sourced_env_file_is_clean():
    """The kuma-push pattern: the script reads the token from a file and embeds nothing."""
    body = '. /etc/kuma-push.env\ncurl -fsS "$PUSH_URL"'
    assert not renders_a_secret(body, ["monitor_bridge_appsec_push_token"])


def test_a_longer_identifier_does_not_match_a_shorter_name():
    assert not renders_a_secret("x = authelia_secret_rotation", ["authelia_secret"])


@pytest.mark.parametrize(
    "task,ok",
    [
        ({"no_log": True}, True),
        ({}, False),
        ({"no_log": False}, False),
        ({"no_log": "true"}, False),  # a string is not the boolean ansible honours
    ],
)
def test_no_log_must_be_the_boolean_true(task, ok):
    assert task_keeps_the_body_out_of_the_log(task) is ok


# --- applied to the tree ----------------------------------------------------------------


def _shell_template_tasks():
    """(role, src, task) for every template task under roles/k8s whose src is a *.sh.j2."""
    for tasks_file in sorted(K8S_ROLES.glob("*/tasks/*.yml")):
        role = tasks_file.parent.parent.name
        for task in walk_tasks(load_tasks(tasks_file)):
            module = next((m for m in TEMPLATE_MODULES if m in task), None)
            if module is None:
                continue
            src = (
                str((task[module] or {}).get("src", ""))
                if isinstance(task[module], dict)
                else ""
            )
            if src.endswith(".sh.j2"):
                yield role, src, task


def _secret_rendering_tasks():
    names = registry_secret_names()
    for role, src, task in _shell_template_tasks():
        template = K8S_ROLES / role / "templates" / src
        assert template.is_file(), (
            f"{role}: template task names {src}, which does not exist"
        )
        hits = renders_a_secret(template.read_text(), names)
        if hits:
            yield role, src, task, hits


def test_the_census_finds_the_known_members():
    found = {(role, src) for role, src, _t, _h in _secret_rendering_tasks()}
    missing = KNOWN_SECRET_SCRIPTS - found
    assert not missing, (
        f"the census no longer finds {sorted(missing)}; found {sorted(found)}. Either the "
        "script moved, its src was renamed, or the registry name it embeds changed — update "
        "KNOWN_SECRET_SCRIPTS, or the guard below is checking nothing."
    )


def test_every_secret_rendering_shell_template_task_has_no_log():
    offenders = [
        f"{role}/tasks: {task.get('name', '?')!r} renders {src} ({', '.join(hits)}) without no_log: true"
        for role, src, task, hits in _secret_rendering_tasks()
        if not task_keeps_the_body_out_of_the_log(task)
    ]
    assert not offenders, (
        "Under `--diff` the template module prints the rendered body to stdout and ansible.log. "
        "Add `no_log: true  # renders the Kuma push token` to:\n  "
        + "\n  ".join(offenders)
    )
