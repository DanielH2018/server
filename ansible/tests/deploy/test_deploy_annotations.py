"""Holds the deploy-annotation chain together end to end.

The chain has four links in three languages, and every break in it is SILENT — Grafana renders
an annotation query that matches nothing exactly as it renders one with no deploys to show:

    deploy.sh                     --logger-->  syslog
    promote.py land-wait          --logger-->  syslog  (promote_lib/wait.py, flux_host.py)
    the soak Job (flux_soak.py)   --stdout-->  its pod log, the cluster's annotation (D5.3b3)
    alloy                         --ships-->   loki-homelab
    dashboards                    --query-->   that Loki, by datasource uid, one query each

So the things worth pinning are the joins, not the parts. A test that only checked "deploy.sh
calls logger" would pass while the query looked for a different string.

This is the same failure class as HA_BAN_SELECTOR, which shipped selecting on an `app` label
the shipper does not emit and reported "no ip_ban events" through a window containing a real ban.
"""

import ast
import json
import re

from _helpers import REPO, load_defaults
from _k8s_render import rendered_texts
from lib import yaml_fast

_REPO = REPO
_ROLE = _REPO / "ansible/roles/k8s/observability"
_GRAFANA = "grafana.yaml.j2"
_DASHBOARDS = "grafana-dashboards.yaml.j2"
# deploy.sh's locked halves: the foreground's and --detach's both call `annotate`.
_DEPLOY_UNDER_LOCKS = _REPO / "scripts/deploy_tools/deploy_lib/under_locks.py"
_DEPLOY_PLAYBOOK = _REPO / "scripts/deploy_tools/deploy_lib/playbook.py"
_DEPLOY_DETACH = _REPO / "scripts/deploy_tools/deploy_lib/detach_run.py"
# A Flux promotion that land.sh waited on: `land-wait` builds the line, and `FluxHost.log`
# runs `logger`, because promote_lib/wait.py starts no subprocess itself.
_PROMOTE = _REPO / "scripts/deploy_tools/promote_lib/wait.py"
_FLUX_HOST = _REPO / "scripts/deploy_tools/flux_host.py"
_EMITTERS = (_DEPLOY_PLAYBOOK, _PROMOTE)
# The two host emitters build the line with `host_lib.deploy_event` (#6164), so the literal the
# query filters on is that function's, and each emitter must call it.
# The soak Job prints the cluster's line, which a second query reads from its pod log.
_SOAK = _REPO / "ansible/roles/k8s/manifests/files/flux_soak.py"


def test_the_query_matches_what_the_deployers_actually_log():
    """The join that nothing else can check.

    The emitters and the annotation query are edited independently, in different languages. If
    the line filter in the expr stops matching the text `logger` writes, every board keeps
    rendering and silently shows no deploys.
    """
    expr = load_defaults(_ROLE)["observability_deploy_annotation_expr"]

    literals = re.findall(r'\|=\s*"([^"]+)"', expr)
    assert literals, f"the expr must carry a line filter to match on: {expr}"

    import host_lib

    line = host_lib.deploy_event("sonarr", "abcd1234")
    for literal in literals:
        assert literal in line, (
            f"deploy_event never writes {literal!r}, which the annotation query filters "
            f"on — the query would match nothing and the boards would show no deploys"
        )
    for emitter in _EMITTERS:
        assert "deploy_event(" in emitter.read_text(), (
            f"{emitter.name} no longer builds its line with host_lib.deploy_event"
        )


def test_the_expr_parses_the_fields_the_annotation_renders():
    """`textFormat` reads a label, and only `logfmt` produces one.

    Without the parser stage the annotation still appears, showing the raw syslog line instead
    of the service list — degraded rather than broken, and easy to miss.
    """

    import grafana_dashboards

    expr = load_defaults(_ROLE)["observability_deploy_annotation_expr"]
    annotation = grafana_dashboards.build_annotation("x", expr)

    field = re.fullmatch(r"\{\{(\w+)\}\}", annotation["textFormat"])
    assert field, "textFormat should render a single parsed field"
    assert "logfmt" in expr, (
        "textFormat reads a parsed label, so the expr needs `| logfmt`"
    )

    import host_lib

    key = field.group(1)
    assert f" {key}=sonarr " in host_lib.deploy_event("sonarr", "abcd1234"), (
        f"deploy_event does not emit a `{key}=` field, so the annotation text would be "
        f"blank on every marker"
    )


def test_the_datasource_uid_matches_the_provisioned_one():
    """A wrong uid renders the annotation against nothing, with no error.

    Same silent shape as the stale-uid class validate/grafana_dashboard_uids.py guards for panels —
    but an injected annotation never appears in the source JSON, so that validator cannot see it.
    """
    uid = load_defaults(_ROLE)["observability_loki_homelab_uid"]

    grafana = next(
        text
        for role, name, text in rendered_texts()
        if (role, name) == ("observability", _GRAFANA)
    )
    block = grafana.split("- name: loki-homelab", 1)
    assert len(block) == 2, (
        "grafana.yaml.j2 no longer provisions a `loki-homelab` datasource"
    )

    provisioned = re.search(r"uid:\s*(\S+)", block[1])
    assert provisioned, "the loki-homelab datasource declares no uid"
    assert provisioned.group(1) == uid, (
        f"observability_loki_homelab_uid ({uid}) and the provisioned datasource "
        f"({provisioned.group(1)}) have drifted"
    )


def test_both_deploy_paths_annotate():
    """One emitter alone means the dashboards show part of the deploys, which is worse than
    none — an operator would read the gaps as "nothing was deployed then". `deploy.sh`
    annotates a hand deploy, and `land-wait` annotates a Flux promotion with
    `source=promote` once it settles. The GitOps deployer's emitter went with its k8s arms in
    D6.12; the name predates that and stays, because
    docs/target-architecture/deploy/d6-delete-old-path.md cites it."""
    assert "def annotate(" in _DEPLOY_PLAYBOOK.read_text()
    promote = _PROMOTE.read_text()
    assert "def annotate_promotion(" in promote and 'source="promote"' in promote
    assert '"deploy-annotation"' in promote


def _annotate_guards(source: str, function: str = "run") -> list[str]:
    """The test of the `if` enclosing each `annotate(...)` call in `function`."""
    tree = ast.parse(source)
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function
    )
    return [
        ast.unparse(branch.test)
        for branch in ast.walk(run)
        if isinstance(branch, ast.If)
        for stmt in branch.body
        for call in ast.walk(stmt)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id == "annotate")
            or (isinstance(call.func, ast.Attribute) and call.func.attr == "annotate")
        )
    ]


def test_the_python_locked_half_annotates_only_on_success():
    """A failed deploy must not leave a marker saying it happened: one `annotate` call per
    mode, behind `status == 0` -- the foreground's `run` and the detached child's
    `deploy_and_gate`, which calls it as `steps.annotate`."""
    assert _annotate_guards(_DEPLOY_UNDER_LOCKS.read_text()) == ["status == 0"]
    assert _annotate_guards(_DEPLOY_DETACH.read_text(), "deploy_and_gate") == [
        "status == 0"
    ]


def test_an_unconditional_annotation_is_flagged():
    assert _annotate_guards("def run(s):\n    status = go(s)\n    annotate(s)\n") == []


def test_annotating_can_never_fail_a_good_deploy():
    """`logger` is absent in a container and can fail on a full disk.

    Neither may turn a successful deploy into a reported failure.
    """
    python = _DEPLOY_PLAYBOOK.read_text()
    annotate = python.split("def annotate(", 1)[1].split("\ndef ", 1)[0]
    assert "contextlib.suppress(OSError)" in annotate, (
        "the wrapper's emitter must swallow a missing or failing logger"
    )

    flux_host = _FLUX_HOST.read_text()
    log = flux_host.split("    def log(", 1)[1].split("\n    def ", 1)[0]
    assert "contextlib.suppress(OSError" in log, (
        "land-wait's logger call must swallow a missing or failing logger"
    )


def test_the_template_injects_both_queries_into_every_board():
    """The whole mechanism is inert if the ConfigMap template stops calling the filter.

    It renders, it applies, and Grafana simply gets boards with no annotation — green
    everywhere. So the rendered ConfigMaps are read back, and every board in them must carry
    both queries with the role's own expressions.
    """
    defaults = load_defaults(_ROLE)
    rendered = next(
        text
        for role, name, text in rendered_texts()
        if (role, name) == ("observability", _DASHBOARDS)
    )
    boards = [
        json.loads(body)
        for doc in yaml_fast.safe_load_all(rendered)
        if doc
        for body in doc["data"].values()
    ]
    assert len(boards) >= 20, "the dashboard ConfigMaps rendered almost no boards"
    want = {
        "Deploys": defaults["observability_deploy_annotation_expr"],
        "Flux deploys": defaults["observability_flux_deploy_annotation_expr"],
    }
    for board in boards:
        found = {
            e["name"]: e["target"]["expr"]
            for e in board["annotations"]["list"]
            if e["name"] in want
        }
        assert found == want, board.get("title")


def test_the_flux_query_matches_what_the_soak_job_prints():
    """The second query's join: its line filter, its parsed field and its container."""
    import grafana_dashboards
    from renderer.renderer_lib import soak

    expr = load_defaults(_ROLE)["observability_flux_deploy_annotation_expr"]
    text = _SOAK.read_text()
    literals = re.findall(r'\|=\s*"([^"]+)"', expr)
    assert literals and all(literal in text for literal in literals), expr
    field = re.fullmatch(
        r"\{\{(\w+)\}\}", grafana_dashboards.build_annotation("x", expr)["textFormat"]
    )
    assert field and "logfmt" in expr and f"{field.group(1)}=" in text
    assert f'container="{soak.SOAK}"' in expr and 'namespace="flux-system"' in expr
