"""A template task that renders a secret into a host script carries `no_log: true`.

`ansible.builtin.template` puts the rendered body in its result only under diff mode, and
`scripts/deploy.sh` forwards the operator's flags to ansible-playbook unchanged, so a
`--check --diff` rehearsal prints every changed script's before/after to stdout and to
`ansible.log` (`log_path = ./ansible.log`, ansible.cfg). A script that interpolates a Kuma push
token is therefore a credential in the log the moment someone types `--diff`. `no_log: true` is
the one setting that keeps the body out of the callback whatever the flags say, and
`.claude/rules/ansible.md` requires it on any task that handles a secret.

The census: every `template` task under `roles/{k8s,setup}/*/tasks/` whose `src` is a
`*.sh.j2`, whose body references a name in `ansible/secret_rotation.yml` INSIDE a Jinja
expression. A whole-body match would flag a script that only names a secret in a comment —
`ups-secondary-health.sh.j2` says where `nut_monitor_password` lives and reads it from
`/etc/nut/upsmon.conf` — and a comment renders nothing. The registry is plaintext by design
(names, never values), so this runs on a CI runner with no SOPS key. The name list
(`_helpers.registry_secret_names`) is shared with the release_bin guard so the two cannot
disagree about what counts as a secret.

LIMIT: a script rendering a name the registry does not carry is invisible here.
`eval-run.sh.j2` renders `homelab_eval_push_token` and `anthropic_api_key`, neither of which
exists in SOPS yet, so its task carries `no_log` by hand and joins KNOWN_SECRET_SCRIPTS only
when the token is minted and registered.

WHY THIS IS NOT A CENSUS ROW (#3430). A row judges one subject from its own text, but this
predicate reads a second file: the template a task's `src` names. A red fixture would have to be
a real template on disk, and the unit tests below pin where a Jinja delimiter starts and ends,
which a row's red/green pair would not carry.

Run: uv run pytest ansible/tests/repo/test_secret_rendering_host_scripts_have_no_log.py
"""

import re

import pytest
from _helpers import ROLES, load_tasks, registry_secret_names, walk_tasks

TEMPLATE_MODULES = {"ansible.builtin.template", "template"}
PLANES = ("k8s", "setup")

# Named so the census cannot go vacuous: a rename of the tasks
# file, of the src, or of the registry entry the body references drops the script out of the
# set silently, and the assertion below would pass over nothing.
KNOWN_SECRET_SCRIPTS = frozenset(
    {
        ("k8s", "crowdsec", "crowdsec-update-home-allowlist.sh.j2"),
        ("k8s", "crowdsec", "crowdsec-appsec-verify.sh.j2"),
        ("k8s", "janitorr", "janitorr-health.sh.j2"),
        ("k8s", "configarr", "configarr-health.sh.j2"),
        ("k8s", "registry", "registry-gc.sh.j2"),
        ("setup", "initial_setup", "docs-refresh.sh.j2"),
        ("setup", "optimize_pi", "pi-sd-health.sh.j2"),
        ("setup", "optimize_pi", "pi-recovery-health.sh.j2"),
        ("setup", "gitops_deploy", "github-ruleset-drift.sh.j2"),
        ("setup", "gitops_deploy", "github-interaction-limit.sh.j2"),
    }
)


# --- the rule, as a predicate ----------------------------------------------------------


def renders_a_secret(body: str, names) -> list[str]:
    """The registry names `body` references as whole words inside a `{{ … }}` or `{% … %}`.

    A mention outside a Jinja delimiter — a comment saying which secret a config file holds —
    renders nothing, so it is not a hit.
    """
    return sorted(
        n for n in names if re.search(rf"\{{[{{%][^}}]*\b{re.escape(n)}\b", body)
    )


def task_keeps_the_body_out_of_the_log(task: dict) -> bool:
    return task.get("no_log") is True


# --- red proofs -----------------------------------------------------------------------


def test_a_push_url_line_is_flagged():
    body = 'PUSH_URL="https://kuma/api/push/{{ monitor_bridge_appsec_push_token }}"'
    assert renders_a_secret(body, ["monitor_bridge_appsec_push_token"])


def test_a_filtered_expression_is_flagged():
    body = "TOKEN=\"{{ docs_refresh_push_token | default('') }}\""
    assert renders_a_secret(body, ["docs_refresh_push_token"])


def test_a_statement_tag_is_flagged():
    body = "{% if nut_monitor_password %}\nMONITOR x\n{% endif %}"
    assert renders_a_secret(body, ["nut_monitor_password"])


def test_a_sourced_env_file_is_clean():
    """The kuma-push pattern: the script reads the token from a file and embeds nothing."""
    body = '. /etc/kuma-push.env\ncurl -fsS "$PUSH_URL"'
    assert not renders_a_secret(body, ["monitor_bridge_appsec_push_token"])


def test_a_comment_only_mention_is_clean():
    """The ups-secondary-health shape: the comment names the secret, the script reads a file."""
    body = (
        "# The MONITOR line carries nut_monitor_password in field 5.\n"
        'PASS="$(awk \'$1=="MONITOR"{print $5}\' /etc/nut/upsmon.conf)"\n'
    )
    assert not renders_a_secret(body, ["nut_monitor_password"])


def test_a_longer_identifier_does_not_match_a_shorter_name():
    assert not renders_a_secret(
        "x = {{ authelia_secret_rotation }}", ["authelia_secret"]
    )


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
    """(plane, role, src, task) for every template task on either plane whose src is *.sh.j2."""
    for plane in PLANES:
        for tasks_file in sorted((ROLES / plane).glob("*/tasks/*.yml")):
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
                    yield plane, role, src, task


def _secret_rendering_tasks():
    names = registry_secret_names()
    for plane, role, src, task in _shell_template_tasks():
        template = ROLES / plane / role / "templates" / src
        assert template.is_file(), (
            f"{plane}/{role}: template task names {src}, which does not exist"
        )
        hits = renders_a_secret(template.read_text(), names)
        if hits:
            yield plane, role, src, task, hits


def test_the_census_finds_the_known_members():
    found = {
        (plane, role, src) for plane, role, src, _t, _h in _secret_rendering_tasks()
    }
    missing = KNOWN_SECRET_SCRIPTS - found
    assert not missing, (
        f"the census no longer finds {sorted(missing)}; found {sorted(found)}. Either the "
        "script moved, its src was renamed, or the registry name it embeds changed — update "
        "KNOWN_SECRET_SCRIPTS, or the guard below is checking nothing."
    )


def test_every_secret_rendering_shell_template_task_has_no_log():
    offenders = [
        f"{plane}/{role}/tasks: {task.get('name', '?')!r} renders {src} ({', '.join(hits)}) "
        "without no_log: true"
        for plane, role, src, task, hits in _secret_rendering_tasks()
        if not task_keeps_the_body_out_of_the_log(task)
    ]
    assert not offenders, (
        "Under `--diff` the template module prints the rendered body to stdout and ansible.log. "
        "Add `no_log: true  # renders the Kuma push token` to:\n  "
        + "\n  ".join(offenders)
    )
