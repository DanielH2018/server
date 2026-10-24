"""H6.1b's two Kustomization keys in the renderer: `flux_health_check_exprs` and `flux_timeout`.

A role that sets them gets `spec.healthCheckExprs` and its own `spec.timeout` on `<tag>`, and a
role that sets neither renders neither. Each fixture role is a `defaults/main.yml` under
`tmp_path`, which `bundle_files` reads as `roles`. The plan is
docs/target-architecture/host/h6-k3s-suc.md, *The Plans' outcome reaches `flux/system-upgrade`*.

Run: uv run pytest scripts/renderer/tests/test_renderer_health_exprs.py
"""

from lib import yaml_fast
from lib.k8s_roles import role_fields
from renderer.renderer_lib.bundle import bundle_files
from renderer.renderer_lib.ordering import NO_ORDERING

TIMEOUT = "600s"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
CONFIGMAP = "kind: ConfigMap\nmetadata:\n  name: x\n  namespace: homelab\n"
WITH = """\
pin: v1.2.3+k3s1
flux_timeout: 100m
flux_health_check_exprs:
  - apiVersion: example.io/v1
    kind: Widget
    inProgress: "status.version != '{{ pin | replace('+', '-') }}'"
    current: "status.version == '{{ pin | replace('+', '-') }}'"
"""


def _roles(tmp_path, **defaults: str):
    for tag, text in defaults.items():
        (tmp_path / tag / "defaults").mkdir(parents=True)
        (tmp_path / tag / "defaults" / "main.yml").write_text(text)
    return tmp_path


def _spec(roles, tag: str) -> dict:
    files = bundle_files(
        {"with": {"x.yaml": CONFIGMAP}, "without": {"x.yaml": CONFIGMAP}},
        {},
        ["with", "without"],
        TIMEOUT,
        DOTENV,
        (),
        NO_ORDERING,
        roles=roles,
    )
    [doc] = yaml_fast.safe_load_all(files[f"flux/{tag}.yaml"])
    return doc["spec"]


def test_a_role_with_both_keys_renders_its_expressions_and_its_timeout(tmp_path):
    spec = _spec(_roles(tmp_path, **{"with": WITH, "without": "x: 1\n"}), "with")
    assert spec["timeout"] == "100m"
    assert spec["healthCheckExprs"] == [
        {
            "apiVersion": "example.io/v1",
            "kind": "Widget",
            "inProgress": "status.version != 'v1.2.3-k3s1'",
            "current": "status.version == 'v1.2.3-k3s1'",
        }
    ]


def test_a_role_without_the_keys_renders_neither(tmp_path):
    spec = _spec(_roles(tmp_path, **{"with": WITH, "without": "x: 1\n"}), "without")
    assert spec["timeout"] == TIMEOUT
    assert "healthCheckExprs" not in spec


def test_a_flux_timeout_wins_over_the_rollout_timeout(tmp_path):
    roles = _roles(
        tmp_path, **{"with": "manifests_rollout_timeout: 5m\nflux_timeout: 9m\n"}
    )
    fields, problems = role_fields(["with"], roles)
    assert problems == [] and fields["with"]["timeout"] == "9m"


def test_a_malformed_key_is_a_problem_and_the_tag_is_left_out(tmp_path):
    roles = _roles(
        tmp_path,
        bad_timeout="flux_timeout: soon\n",
        bad_list="flux_health_check_exprs: nope\n",
        bad_item="flux_health_check_exprs:\n  - {apiVersion: a/v1, kind: K, current: x, extra: y}\n",
        bad_name="flux_health_check_exprs:\n  - {apiVersion: a/v1, current: '{{ nope }}'}\n",
    )
    fields, problems = role_fields(
        ["bad_timeout", "bad_list", "bad_item", "bad_name"], roles
    )
    assert fields == {}
    assert len(problems) == 4
