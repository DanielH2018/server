"""The k3s scaffolder writes a role that renders, and an entry the inventory can still parse.

WHY THIS EXISTS. `new_k8s_service.py` is step 1 of the `new-k8s-service` skill, so its output
is the starting point every new service inherits. A generated Deployment that fails to
render, or an entry appended where `containers_list` no longer parses, is a failure the
author meets only after they have already written the rest of the service.

The rendering half goes through `validate.k8s_manifests`'s own machinery rather than a second
Jinja environment, so what these tests accept cannot drift from what the repo's validator
accepts.

Run: uv run pytest scripts/dev/tests/test_new_k8s_service.py
"""

import pytest

from lib import yaml_fast
from lib.ansible_jinja_env import template_env
from lib.k8s_roles import (
    SHARED_MANIFEST_DEFAULTS,
    declared_manifest_files,
    resolved_manifest_files,
)
from lib.render_guard import StubUndefined
from validate.k8s_manifests import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_HOST_VARS,
    load_yaml,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
)

import new_k8s_service as scaffold


@pytest.fixture
def args():
    return scaffold.parse_args(
        [
            "widget",
            "--image",
            "ghcr.io/example/widget:1.0.0",
            "--port",
            "8080",
            "--authelia",
            "one_factor",
            "--uid",
            "101",
        ]
    )


def _render(tmp_path, text: str, name: str, port: int, defaults=None) -> list[dict]:
    """Render a generated manifest the way `validate/k8s_manifests.py` renders a real one.

    Written to a temporary templates directory first, because the validator's environment
    loads a role's own directory alongside the shared macros — rendering the string on its
    own would not prove the `{% from %}` lines resolve the way a deploy resolves them.

    Returns every parsed document: the ingressroute macro emits an IngressRoute and the
    Middlewares beside it, so a caller selecting on `kind` reads the one it means.
    """
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(K8S_HOST_VARS),
        "playbook_dir": str(ANSIBLE),
    }
    ctx = resolve_vars(base, base)
    ctx["container_item"] = {"name": name, "hostname": name, "port": port}
    ctx.update(defaults or {})
    (tmp_path / "generated.yaml.j2").write_text(text)
    env = template_env(tmp_path, undefined_cls=StubUndefined)
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, "generated.yaml.j2", ctx)
    assert rendered is not None, f"generated template failed to render: {err}"
    return [d for d in yaml_fast.safe_load_all(rendered) if isinstance(d, dict)]


def test_the_variable_prefix_follows_ansible_lints_rule():
    assert scaffold.var_prefix("bento-pdf") == "bento_pdf"
    assert scaffold.var_prefix("jellyfin") == "jellyfin"


def test_the_generated_deployment_renders_and_pins_a_uid(args, tmp_path):
    defaults = yaml_fast.safe_load(
        scaffold.defaults_main(
            args.name, args.image, args.autodeploy, args.autodeploy_reason, args.uid
        )
    )
    assert defaults["widget_k8s_uid"] == 101
    assert defaults["widget_k8s_image"] == "ghcr.io/example/widget:1.0.0"
    assert defaults["k8s_autodeploy"] is True

    text = scaffold.deployment_template(args.name, args.strategy, args.priority_class)
    doc = next(
        d
        for d in _render(tmp_path, text, args.name, args.port, defaults)
        if d["kind"] == "Deployment"
    )
    assert doc["kind"] == "Deployment"
    pod = doc["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 101, (
        "the pod pins no runAsUser, so the generated role fails "
        "test_every_asserting_container_pins_a_uid out of the box"
    )
    container = pod["containers"][0]
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["ports"][0]["containerPort"] == 8080


def _entry(args) -> dict:
    return yaml_fast.safe_load(
        scaffold.entry_lines(
            args.name, args.port, args.hostname, args.authelia, args.route
        )
    )[0]


def test_the_tasks_name_no_file_list(args):
    """`k8s/manifests` derives the list; a scaffolded one would only restate it."""
    tasks = yaml_fast.safe_load(scaffold.tasks_main(args.name))
    assert tasks[0]["vars"] == {"manifests_rollout": args.name}


def test_the_entry_earns_the_shared_defaults_without_a_template(args, tmp_path):
    """The derivation adds a shared default for the entry's `port` and `hostname`."""
    scaffold.write_role(args, tmp_path)
    files, secret = resolved_manifest_files("widget", tmp_path, _entry(args))
    assert files == {"deployment.yaml", "service.yaml", "ingressroute.yaml"}
    assert secret == set()
    assert {"service.yaml", "ingressroute.yaml"} <= SHARED_MANIFEST_DEFAULTS.keys()


def test_an_unrouted_service_gets_no_ingressroute_and_no_hostname(tmp_path):
    args = scaffold.parse_args(
        ["widget", "--image", "img:1", "--port", "9000", "--no-route"]
    )
    entry = _entry(args)
    scaffold.write_role(args, tmp_path)
    files, _secret = resolved_manifest_files("widget", tmp_path, entry)
    assert files == {"deployment.yaml", "service.yaml"}
    assert "hostname" not in entry
    assert "use_authelia" not in entry


def test_authelia_writes_the_tier_beside_the_flag(args):
    entry = yaml_fast.safe_load(
        scaffold.entry_lines(args.name, args.port, args.hostname, args.authelia, True)
    )[0]
    assert entry == {
        "name": "widget",
        "platform": "k8s",
        "hostname": "widget",
        "port": 8080,
        "use_authelia": True,
        "auth_tier": "one_factor",
    }


def test_the_entry_lands_inside_containers_list(tmp_path):
    """The bug the first run hit: appended at the end of the file, it parses as nothing."""
    box = tmp_path / "daniel-box.yml"
    box.write_text(
        "---\nk8s_namespace: homelab\ncontainers_list:\n"
        "  - name: alpha\n    platform: k8s\n    port: 1\n"
        "\n# A comment above the next key.\nrenovate_agent_enabled: true\n"
    )
    scaffold.append_entry(
        box, scaffold.entry_lines("widget", 8080, "widget", None, False)
    )
    loaded = yaml_fast.safe_load(box.read_text())
    assert [c["name"] for c in loaded["containers_list"]] == ["alpha", "widget"]
    assert loaded["renovate_agent_enabled"] is True


def test_appending_to_a_file_with_no_containers_list_is_refused(tmp_path):
    """Control: the walk needs its anchor, and a missing one is not "append at the end"."""
    box = tmp_path / "daniel-box.yml"
    box.write_text("---\nk8s_namespace: homelab\n")
    with pytest.raises(ValueError, match="declares no containers_list"):
        scaffold.append_entry(box, "  - name: widget\n")


def test_authelia_without_a_route_is_refused(capsys):
    """A middleware with nothing to attach to is a command-line mistake, not a default."""
    code = scaffold.main(
        [
            "widget",
            "--image",
            "img:1",
            "--port",
            "1",
            "--no-route",
            "--authelia",
            "one_factor",
        ]
    )
    assert code == 1
    assert "--no-route writes" in capsys.readouterr().err


def test_the_generated_role_ships_no_service_or_route_template(args, tmp_path):
    """Four files, and neither a Service nor an IngressRoute."""
    written = {
        p.relative_to(tmp_path / "widget").as_posix()
        for p in scaffold.write_role(args, tmp_path)
    }
    assert written == {
        "tasks/main.yml",
        "defaults/main.yml",
        "templates/deployment.yaml.j2",
        "CLAUDE.md",
    }


def test_the_real_tree_agrees_that_a_scaffolded_role_takes_the_default(args, tmp_path):
    """Non-vacuity: the fallback resolver, run over the generated role, finds the Service."""
    scaffold.write_role(args, tmp_path)
    declared = declared_manifest_files("widget", tmp_path, _entry(args))
    assert "service.yaml" in declared
    assert "ingressroute.yaml" in declared
