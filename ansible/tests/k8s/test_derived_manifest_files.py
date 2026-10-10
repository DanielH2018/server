"""`k8s/manifests` derives a role's file lists, and the offline harnesses derive the same ones.

A role that passes no `manifests_files` gets one derived from its own `templates/` and its
containers_list entry (#3662). Two copies of that rule exist and must agree. The deploy's copy
is the "Resolve the manifest file lists" task in `roles/k8s/manifests/tasks/main.yml`. The
offline harnesses' copy is `lib.k8s_roles.resolved_manifest_files`, which the validator, the
render corpus and the prune guards read. If the two drift, a deploy renders a file the
harnesses never check, or the harnesses check one the deploy never applies. Neither side
fails.

The tests evaluate the shipped task through Ansible's own templar, so they read the
expressions the deploy runs rather than a restatement of them.

Run: uv run pytest ansible/tests/k8s/test_derived_manifest_files.py
"""

from pathlib import Path

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from lib.k8s_roles import (
    SECRET_MANIFEST_NAME,
    SKIP_ROLES,
    k8s_entries,
    resolved_manifest_files,
    role_dirs,
)
from lib import yaml_fast
from _helpers import ANSIBLE, load_tasks
from _k8s_render import rendered_texts

MANIFESTS_TASKS = ANSIBLE / "roles" / "k8s" / "manifests" / "tasks" / "main.yml"
RESOLVE_TASK = "Resolve the manifest file lists"

# The include vars the resolve task reads. Nothing else a caller passes reaches it.
FILE_LIST_KEYS = (
    "manifests_files",
    "manifests_secret_files",
    "manifests_exclude_files",
    "manifests_deferred_files",
)

# Roles whose include builds a list with a Jinja expression over role vars. The deploy
# evaluates it; the offline copy reads every basename in it, over-inclusively by design, so the
# two are not comparable and these roles are left out of the agreement test by name.
JINJA_LIST_ROLES = frozenset({"authelia", "navidrome", "traefik"})

# Members the agreement test must reach, one per arm of the rule: a shared Service and route
# (bazarr), a secret by name (sonarr), a one-off Job (registry), a deferred file plus an
# exclusion (pihole), a listed secret file (uptime-kuma) and an excluded shared default
# (scrutiny).
NAMED_MEMBERS = frozenset(
    {"bazarr", "sonarr", "registry", "pihole", "uptime-kuma", "scrutiny"}
)


def _resolve_task() -> dict:
    matches = [
        t
        for t in load_tasks(MANIFESTS_TASKS)
        if t.get("name", "").startswith(RESOLVE_TASK)
    ]
    assert len(matches) == 1, f"{RESOLVE_TASK!r} matched {len(matches)} tasks"
    return matches[0]


def _trusted(value):
    return trust_as_template(value) if isinstance(value, str) else value


def ansible_resolve(
    service: str, include_vars: dict, entry: dict | None, playbook_dir: Path = ANSIBLE
) -> tuple[list[str], list[str]]:
    """`(files, secret_files)` as the shipped resolve task computes them for `service`."""
    task = _resolve_task()
    variables = {k: _trusted(v) for k, v in task["vars"].items()}
    variables |= {
        k: _trusted(v) for k, v in include_vars.items() if k in FILE_LIST_KEYS
    }
    variables |= {"playbook_dir": str(playbook_dir), "manifests_service": service}
    if entry is not None:
        variables["container_item"] = {"name": service, **entry}
    templar = Templar(loader=DataLoader(), variables=variables)
    facts = task["ansible.builtin.set_fact"]
    return (
        list(templar.template(trust_as_template(facts["manifests_files_resolved"]))),
        list(
            templar.template(
                trust_as_template(facts["manifests_secret_files_resolved"])
            )
        ),
    )


def _include_vars(role: Path) -> dict | None:
    """The vars `role`'s tasks/main.yml passes to its render include, or None if it has none."""
    tasks = role / "tasks" / "main.yml"
    if not tasks.is_file():
        return None
    for task in load_tasks(tasks):
        include = task.get("ansible.builtin.include_role") or {}
        if include.get("name") == "k8s/manifests" and "tasks_from" not in include:
            return task.get("vars") or {}
    return None


def _comparable_roles() -> dict[str, dict]:
    roles = {}
    for role in role_dirs(exclude=SKIP_ROLES):
        include_vars = _include_vars(role)
        if include_vars is None or role.name in JINJA_LIST_ROLES:
            continue
        roles[role.name] = include_vars
    return roles


def test_the_deploy_and_the_harnesses_derive_the_same_lists():
    roles = _comparable_roles()
    assert NAMED_MEMBERS <= roles.keys(), NAMED_MEMBERS - roles.keys()
    assert len(roles) >= 50, f"only {len(roles)} roles compared"
    entries = k8s_entries()
    disagree = {}
    for name, include_vars in roles.items():
        files, secret = ansible_resolve(name, include_vars, entries.get(name, {}))
        assert len(files) == len(set(files)) and len(secret) == len(set(secret)), name
        expected = resolved_manifest_files(name)
        if (set(files), set(secret)) != expected:
            disagree[name] = {"ansible": (files, secret), "python": expected}
    assert not disagree, disagree


def test_the_jinja_list_roles_still_build_their_lists_with_jinja():
    """A role that stops needing its Jinja list joins the agreement test instead."""
    for name in JINJA_LIST_ROLES:
        include_vars = _include_vars(ANSIBLE / "roles" / "k8s" / name)
        assert include_vars is not None, name
        assert "{{" in str(include_vars.get("manifests_files", "")), (
            f"{name} no longer builds manifests_files with Jinja; drop it from "
            "JINJA_LIST_ROLES so the agreement test covers it."
        )


def _widget(tmp_path: Path, *templates: str) -> Path:
    """A playbook dir holding one role, `widget`, that ships `templates`."""
    tpl_dir = tmp_path / "roles" / "k8s" / "widget" / "templates"
    tpl_dir.mkdir(parents=True)
    (tmp_path / "roles" / "k8s" / "widget" / "tasks").mkdir()
    for name in templates:
        (tpl_dir / name).write_text("---\n")
    return tmp_path


def _both(tmp_path, include_vars, entry):
    """The two copies' answers for `widget`, each as sorted lists."""
    ansible = ansible_resolve("widget", include_vars, entry, tmp_path)
    tasks = tmp_path / "roles" / "k8s" / "widget" / "tasks" / "main.yml"
    body = "".join(f"    {k}: {v!r}\n" for k, v in include_vars.items())
    tasks.write_text(f"---\n- name: Deploy widget\n  vars:\n{body}")
    python = resolved_manifest_files("widget", tmp_path / "roles" / "k8s", entry)
    return (
        (sorted(ansible[0]), sorted(ansible[1])),
        (sorted(python[0]), sorted(python[1])),
    )


def test_a_derived_role_renders_its_templates_and_earns_its_shared_defaults(tmp_path):
    _widget(
        tmp_path,
        "deployment.yaml.j2",
        "config-secret.yaml.j2",
        "probe-job.yaml.j2",
        "later.yaml.j2",
        "Dockerfile.j2",
        "health.sh.j2",
    )
    ansible, python = _both(
        tmp_path,
        {"manifests_deferred_files": ["later.yaml"]},
        {"port": 80, "hostname": "w"},
    )
    expected = (
        ["deployment.yaml", "ingressroute.yaml", "service.yaml"],
        ["config-secret.yaml"],
    )
    assert ansible == python == expected


def test_an_entry_with_no_port_or_hostname_earns_no_shared_default(tmp_path):
    _widget(tmp_path, "deployment.yaml.j2")
    ansible, python = _both(tmp_path, {}, {})
    assert ansible == python == (["deployment.yaml"], [])


def test_an_exclusion_drops_a_template_and_a_shared_default(tmp_path):
    _widget(tmp_path, "deployment.yaml.j2", "macro.yaml.j2")
    ansible, python = _both(
        tmp_path,
        {"manifests_exclude_files": ["macro.yaml", "service.yaml"]},
        {"port": 80},
    )
    assert ansible == python == (["deployment.yaml"], [])


def test_a_role_that_ships_its_own_service_takes_no_shared_one(tmp_path):
    _widget(tmp_path, "deployment.yaml.j2", "service.yaml.j2")
    ansible, python = _both(tmp_path, {}, {"port": 80})
    assert ansible == python == (["deployment.yaml", "service.yaml"], [])


def test_a_listed_file_list_wins_over_the_derivation(tmp_path):
    _widget(tmp_path, "deployment.yaml.j2", "extra.yaml.j2", "secret.yaml.j2")
    ansible, python = _both(
        tmp_path,
        {
            "manifests_files": ["deployment.yaml"],
            "manifests_secret_files": ["extra.yaml"],
        },
        {"port": 80},
    )
    assert ansible == python == (["deployment.yaml"], ["extra.yaml"])


def test_a_listed_secret_file_leaves_the_derived_files_list(tmp_path):
    """uptime-kuma's shape: a Secret whose name does not say so, listed by hand."""
    _widget(tmp_path, "deployment.yaml.j2", "secret.yaml.j2", "monitors.yaml.j2")
    ansible, python = _both(
        tmp_path, {"manifests_secret_files": ["secret.yaml", "monitors.yaml"]}, {}
    )
    assert ansible == python == (["deployment.yaml"], ["monitors.yaml", "secret.yaml"])


# The fail-safe for deriving secrecy from a name. A template that renders a Secret but is not
# named as one renders 0644 and outside no_log, with its decrypted values in the task output.
def world_readable_secrets(renders, resolve) -> list[str]:
    """`<role>/<template>` for every render that holds a Secret yet stages world-readable.

    `renders` yields `(role, template name, rendered text)`; `resolve(role)` returns the role's
    `(files, secret_files)`.
    """
    found = []
    for role, name, text in renders:
        if name.removesuffix(".j2") not in resolve(role)[0]:
            continue
        docs = [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]
        if any(d.get("kind") == "Secret" for d in docs):
            found.append(f"{role}/{name}")
    return sorted(found)


def test_no_role_renders_a_secret_world_readable_is_clean():
    renders = rendered_texts()
    assert {
        ("uptime-kuma", "static-monitors.yaml.j2"),
        ("sonarr", "secret-exportarr.yaml.j2"),
    } <= {(role, name) for role, name, _text in renders}, (
        "the render corpus no longer carries the two Secret templates this guard is sized on"
    )
    assert world_readable_secrets(renders, resolved_manifest_files) == []


def test_a_secret_template_named_as_a_manifest_is_flagged():
    renders = [
        ("widget", "monitors.yaml.j2", "---\napiVersion: v1\nkind: Secret\n"),
        ("widget", "secret.yaml.j2", "---\napiVersion: v1\nkind: Secret\n"),
    ]

    def resolve(_role):
        return {"monitors.yaml"}, {"secret.yaml"}

    assert world_readable_secrets(renders, resolve) == ["widget/monitors.yaml.j2"]


@pytest.mark.parametrize(
    ("name", "is_secret"),
    [
        ("secret.yaml", True),
        ("config-secret.yaml", True),
        ("secret-exportarr.yaml", True),
        ("secrets-ui.yaml", False),
        ("deployment.yaml", False),
    ],
)
def test_the_secret_name_rule(name, is_secret):
    assert bool(SECRET_MANIFEST_NAME.search(name)) is is_secret
