"""Holds the deploy-annotation chain together end to end.

The chain has four links in three languages, and every break in it is SILENT — Grafana renders
an annotation query that matches nothing exactly as it renders one with no deploys to show:

    deploy.sh / gitops_deploy.py  --logger-->  syslog
    promtail                      --ships-->   loki-homelab
    dashboards                    --query-->   that Loki, by datasource uid

So the things worth pinning are the joins, not the parts. A test that only checked "deploy.sh
calls logger" would pass while the query looked for a different string.

This is the same failure class as HA_BAN_SELECTOR, which shipped selecting on an `app` label
promtail does not emit and reported "no ip_ban events" through a window containing a real ban.
"""

from __future__ import annotations

import re

import yaml
from _helpers import REPO

_REPO = REPO
_DEFAULTS = _REPO / "ansible/roles/k8s/claude-otel/defaults/main.yml"
_GRAFANA = _REPO / "ansible/roles/k8s/claude-otel/templates/grafana.yaml.j2"
_DASHBOARDS_TASKS = _REPO / "ansible/roles/k8s/claude-otel/tasks/dashboards.yml"
_DEPLOY_SH = _REPO / "scripts/deploy.sh"
_GITOPS = _REPO / "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py"


def _defaults() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text())


def test_the_query_matches_what_the_deployers_actually_log():
    """The join that nothing else can check.

    Both emitters and the annotation query are edited independently, in different languages. If
    the line filter in the expr stops matching the text `logger` writes, every board keeps
    rendering and silently shows no deploys.
    """
    expr = _defaults()["claude_otel_deploy_annotation_expr"]

    literals = re.findall(r'\|=\s*"([^"]+)"', expr)
    assert literals, f"the expr must carry a line filter to match on: {expr}"

    for emitter in (_DEPLOY_SH, _GITOPS):
        text = emitter.read_text()
        for literal in literals:
            assert literal in text, (
                f"{emitter.name} never logs {literal!r}, which the annotation query filters "
                f"on — the query would match nothing and the boards would show no deploys"
            )


def test_the_expr_parses_the_fields_the_annotation_renders():
    """`textFormat` reads a label, and only `logfmt` produces one.

    Without the parser stage the annotation still appears, showing the raw syslog line instead
    of the service list — degraded rather than broken, and easy to miss.
    """
    import sys

    sys.path.insert(0, str(_REPO / "scripts"))
    import inject_dashboard_annotations as inject

    expr = _defaults()["claude_otel_deploy_annotation_expr"]
    annotation = inject.build_annotation("x", expr)

    field = re.fullmatch(r"\{\{(\w+)\}\}", annotation["textFormat"])
    assert field, "textFormat should render a single parsed field"
    assert "logfmt" in expr, (
        "textFormat reads a parsed label, so the expr needs `| logfmt`"
    )

    key = field.group(1)
    for emitter in (_DEPLOY_SH, _GITOPS):
        assert f"{key}=" in emitter.read_text(), (
            f"{emitter.name} does not emit a `{key}=` field, so the annotation text would be "
            f"blank on every marker"
        )


def test_the_datasource_uid_matches_the_provisioned_one():
    """A wrong uid renders the annotation against nothing, with no error.

    Same silent shape as the stale-uid class validate_grafana_dashboards.py guards for panels —
    but an injected annotation never appears in the source JSON, so that validator cannot see it.
    """
    uid = _defaults()["claude_otel_loki_homelab_uid"]

    grafana = _GRAFANA.read_text()
    block = grafana.split("- name: loki-homelab", 1)
    assert len(block) == 2, (
        "grafana.yaml.j2 no longer provisions a `loki-homelab` datasource"
    )

    provisioned = re.search(r"uid:\s*(\S+)", block[1])
    assert provisioned, "the loki-homelab datasource declares no uid"
    assert provisioned.group(1) == uid, (
        f"claude_otel_loki_homelab_uid ({uid}) and the provisioned datasource "
        f"({provisioned.group(1)}) have drifted"
    )


def test_both_deploy_paths_annotate():
    """One emitter alone means the dashboards show half the deploys, which is worse than none —
    an operator would read the gaps as "nothing was deployed then"."""
    assert "emit_deploy_annotation" in _DEPLOY_SH.read_text()
    assert "emit_deploy_annotation" in _GITOPS.read_text()


def test_deploy_sh_annotates_only_on_success():
    """A failed deploy must not leave a marker saying it happened."""
    body = _DEPLOY_SH.read_text()
    func = body.split("emit_deploy_annotation() {", 1)[1].split("\n}", 1)[0]

    assert re.search(r'\[\[\s*"\$status"\s*==\s*0\s*\]\]', func), (
        "emit_deploy_annotation must return early unless the deploy exited 0"
    )


def test_annotating_can_never_fail_a_good_deploy():
    """`logger` is absent in a container and can fail on a full disk.

    Neither may turn a successful deploy into a reported failure.
    """
    body = _DEPLOY_SH.read_text()
    func = body.split("emit_deploy_annotation() {", 1)[1].split("\n}", 1)[0]
    assert "|| true" in func, "the logger call must be fire-and-forget"

    gitops = _GITOPS.read_text()
    impl = gitops.split("def emit_deploy_annotation(", 1)[1].split("\ndef ", 1)[0]
    assert "except Exception" in impl, (
        "the gitops emitter must swallow its own failures"
    )


def test_the_configmap_is_built_from_the_injected_tree():
    """The whole mechanism is inert if the ConfigMap still reads the pristine staged tree.

    It renders, it applies, and Grafana simply gets boards with no annotation — green
    everywhere. Reading the staged path here is the single most likely way to half-land this.
    """
    tasks = _DASHBOARDS_TASKS.read_text()

    build = tasks.split("Build a ConfigMap manifest per dashboard folder", 1)
    assert len(build) == 2
    block = build[1].split("- name:", 1)[0]

    assert "--from-file=/etc/rancher/k3s/dashboards-annotated/" in block, (
        "the ConfigMap must be built from the injector's output tree, not the staged source"
    )


def test_the_injector_writes_a_derived_tree_not_the_staged_one():
    """Injecting in place makes the staging copy task fight the injector.

    It restores the pristine JSON every deploy, the injector re-injects, and Grafana rolls every
    time.
    """
    tasks = _DASHBOARDS_TASKS.read_text()
    step = tasks.split("Inject the deploy-annotation query", 1)
    assert len(step) == 2, "dashboards.yml no longer injects the annotation"
    block = step[1].split("- name:", 1)[0]

    src = re.search(r"--src\s+(\S+)", block)
    dest = re.search(r"--dest\s+(\S+)", block)
    assert src and dest, "the injector needs both --src and --dest"
    assert src.group(1) != dest.group(1), (
        "src and dest must differ — an in-place edit makes the staging copy task and the "
        "injector overwrite each other on every deploy"
    )
