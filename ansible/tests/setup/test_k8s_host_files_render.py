"""Every `k8s_host_files` template renders under `StrictUndefined` from its own role's context.

`k8s_host_files` takes host-side task files out of k8s roles (D5's *Host-side rows*). A
moved template that still reads its old role's default renders in no deploy: role defaults
do not resolve across roles, so Ansible fails the task that reads the name. The lenient
setup-plane guard would render it as `STUB`. This test renders each template under
`StrictUndefined` instead, with the role's defaults, `group_vars/all.yml` and daniel-box's
host_vars, through `lib.ansible_jinja_env`, so the read fails here first.

`EXPECTED_TEMPLATES` names the templates the live role ships, which makes the census
non-vacuous. pi-peer-backup's flip, which the plan expected to add the first, moved a static
wrapper and a public key into `files/` instead. crowdsec's first conversion PR added the edge
probe's service and timer units, which H5.14 moved into the Pi package.

Red-proof: a fixture role under `tmp_path`, laid out like the real one, whose template reads
`artifacts_host_dir`. Only `roles/k8s/artifacts/defaults/main.yml` defines that name, so the
render must fail naming it. A green control in the same tree reads a `group_vars` key and
must render, so the red half cannot pass because every render fails.

Run: uv run pytest ansible/tests/setup/test_k8s_host_files_render.py
"""

from pathlib import Path

import jinja2

from _helpers import K8S_ROLES, SETUP_ROLES, load_defaults, load_yaml
from lib.ansible_jinja_env import template_env
from lib.render_context import render_context
from lib.render_guard import render_or_error
from lib.repo_paths import ALL_VARS
from validate.setup_templates import ANSIBLE_RUNTIME_CONTEXT

ROLE = SETUP_ROLES / "k8s_host_files"

# The templates the live role ships.
EXPECTED_TEMPLATES: frozenset[str] = frozenset(
    {
        "dockerhub-mirror-gc-job.yaml.j2",
        "ghcr-mirror-gc-job.yaml.j2",
        "registry-gc.sh.j2",
    }
)

# A name only a k8s role's defaults define, which a moved template could still read.
K8S_ONLY_VAR = "artifacts_host_dir"


def _templates(role_dir: Path) -> list[Path]:
    """Every template under the role's `templates/`, at any depth."""
    return sorted((role_dir / "templates").rglob("*.j2"))


def render_failures(role_dir: Path) -> dict[str, str]:
    """{template: error} for each template under `role_dir` that fails a strict render.

    The context is the role's defaults over `group_vars/all.yml` and daniel-box's host_vars,
    the host the playbook entry gates the role to.
    """
    ctx = render_context(
        role_dir,
        host="daniel-box",
        overrides=ANSIBLE_RUNTIME_CONTEXT,
        undefined_cls=jinja2.StrictUndefined,
    )
    failures = {}
    for tpl in _templates(role_dir):
        env = template_env(tpl.parent, undefined_cls=jinja2.StrictUndefined)
        rendered, err = render_or_error(env, tpl.name, ctx)
        if rendered is None:
            failures[str(tpl.relative_to(role_dir))] = str(err)
    return failures


def test_the_live_role_ships_exactly_the_expected_templates():
    assert ROLE.is_dir(), f"no such role: {ROLE}"
    found = {str(t.relative_to(ROLE / "templates")) for t in _templates(ROLE)}
    assert found == EXPECTED_TEMPLATES, (
        "the k8s_host_files template set moved: name each new template in "
        f"EXPECTED_TEMPLATES. Found {sorted(found)}"
    )


def test_every_live_template_renders_strictly():
    assert render_failures(ROLE) == {}


def _fixture_role(tmp_path: Path, body: str) -> Path:
    """A role laid out `roles/setup/k8s_host_files/` under `tmp_path`, with one template."""
    role_dir = tmp_path / "roles" / "setup" / "k8s_host_files"
    (role_dir / "templates").mkdir(parents=True)
    (role_dir / "templates" / "fixture.conf.j2").write_text(body)
    return role_dir


def test_the_red_fixture_reads_a_name_only_a_k8s_role_defines():
    """The fixture's premise, checked so a moved default cannot turn the red half vacuous."""
    assert K8S_ONLY_VAR in load_defaults(K8S_ROLES / "artifacts")
    assert K8S_ONLY_VAR not in (load_yaml(ALL_VARS) or {})
    if (ROLE / "defaults" / "main.yml").is_file():
        assert K8S_ONLY_VAR not in load_defaults(ROLE)


def test_a_template_reading_a_k8s_role_default_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path, f"dir={{{{ {K8S_ONLY_VAR} }}}}\n")
    failures = render_failures(role_dir)
    assert set(failures) == {"templates/fixture.conf.j2"}, failures
    assert K8S_ONLY_VAR in failures["templates/fixture.conf.j2"]


def test_a_template_reading_a_group_vars_key_is_clean(tmp_path):
    role_dir = _fixture_role(tmp_path, "user={{ sys_user }}\n")
    assert render_failures(role_dir) == {}
