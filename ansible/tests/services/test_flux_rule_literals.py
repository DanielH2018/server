"""Each name observability's `flux` rule group spells out equals what the flux role renders.

`files/rules/flux.yaml` is plain YAML, so the names it reads are literals:

- `FluxControllerDown` has one `absent()` arm per controller, because pod discovery leaves no
  `up` series for a controller with no pod. An arm the rule lacks leaves that controller's
  loss unpaged, so the arms must name every `flux_controllers` entry;
- `FluxNotReady`'s OCIRepository arms name every OCIRepository `sync.yaml.j2` renders with the
  verify drill off: `homelab`, which the root Kustomization follows, and `last-good`, the soak's base.

Run: uv run pytest ansible/tests/services/test_flux_rule_literals.py
"""

import re

from _helpers import K8S_ROLES, load_defaults
from _k8s_render import render_role_template
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "flux.yaml"
FLUX = K8S_ROLES / "flux"
# Non-vacuity: the controllers the rule must name, as the vendored install ships them.
CONTROLLERS = frozenset(
    {"source-controller", "kustomize-controller", "notification-controller"}
)
ABSENT_ARM = re.compile(r'absent\(up\{job="flux", app="([^"]+)"\}\)')
SOURCE_ARM = re.compile(r'customresource_kind="OCIRepository", name="([^"]+)"')


def rule_name_problems(
    rules: str, controllers: set[str], sources: set[str]
) -> list[str]:
    """Why the flux rule group's literal names differ from the flux role's render, or nothing."""
    absent = set(ABSENT_ARM.findall(rules))
    named = set(SOURCE_ARM.findall(rules))
    problems = []
    if absent != controllers:
        problems.append(
            f"FluxControllerDown's absent() arms name {sorted(absent)}, and flux_controllers "
            f"names {sorted(controllers)}"
        )
    if named != sources:
        problems.append(
            f"FluxNotReady's OCIRepository arm names {sorted(named)}, and sync.yaml.j2 renders "
            f"{sorted(sources)}"
        )
    return problems


def rule_inputs() -> tuple[str, set[str], set[str]]:
    """The rule file's text, the role's controller names, and its OCIRepository names."""
    controllers = {c["name"] for c in load_defaults(FLUX)["flux_controllers"]}
    assert controllers == CONTROLLERS
    sync = render_role_template("flux", "sync.yaml.j2", {"flux_verify_drill": False})
    sources = {
        doc["metadata"]["name"]
        for doc in yaml_fast.safe_load_all(sync)
        if isinstance(doc, dict) and doc.get("kind") == "OCIRepository"
    }
    assert sources == {"homelab", "last-good"}
    return RULES.read_text(), controllers, sources


def test_rule_name_problems_is_clean_on_the_flux_rule_group():
    assert rule_name_problems(*rule_inputs()) == []


def test_rule_name_problems_is_flagged_on_a_controller_the_rule_misses():
    rules, controllers, sources = rule_inputs()
    rules = rules.replace(
        'or absent(up{job="flux", app="notification-controller"})', ""
    )
    problems = rule_name_problems(rules, controllers, sources)
    assert len(problems) == 1 and "notification-controller" in problems[0], problems


def test_rule_name_problems_is_flagged_on_a_source_the_role_does_not_render():
    rules, controllers, _sources = rule_inputs()
    problems = rule_name_problems(rules, controllers, {"homelab-manifests"})
    assert len(problems) == 1 and "homelab-manifests" in problems[0], problems
