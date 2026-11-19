"""The census rows over the observability alert rules: the deploy-pipeline and Flux rules."""

import re

from _helpers import REPO
from _row_table import Census, Subject, tracked
from lib import yaml_fast


def _real(rel: str) -> str:
    return (REPO / rel).read_text(errors="replace")


# ── The observability rules ───────────────────────────────────────────────────────────

_RULES = "k8s/roles/observability/files/rules"
_DEPLOY_RULES = f"{_RULES}/deploy-pipeline.yaml"
_FLUX_RULES = f"{_RULES}/flux.yaml"
_SCRAPE_RULES = f"{_RULES}/scrape-targets.yaml"
_WINDOW_GAUGE = "homelab_deploy_reboot_window_active"
_GAUGE_SELECTOR = _WINDOW_GAUGE + r"(\{[^}]*\})?"
_STALLED_PATTERN = re.compile(
    r"and\s+on\s*\(\s*\)\s*\(\s*" + _GAUGE_SELECTOR + r"\s*==\s*0\s*\)"
)
_ABSENT_PATTERN = re.compile(r"absent\(\s*" + _GAUGE_SELECTOR + r"\s*\)")


def _alert_exprs(text: str, alert: str) -> list[str]:
    groups = yaml_fast.safe_load(text)["groups"]
    return [
        rule["expr"]
        for g in groups
        for rule in g["rules"]
        if rule.get("alert") == alert
    ]


def _alert_expr_offence(alert: str, pattern: re.Pattern[str], want: str):
    def offence(subject: Subject) -> list[str]:
        exprs = _alert_exprs(subject.text, alert)
        if len(exprs) != 1:
            return [f"{len(exprs)} rules named {alert}, want 1"]
        if pattern.search(exprs[0]):
            return []
        return [f"{alert} expr {exprs[0]!r} does not {want}"]

    return offence


def _rules_file(alert: str, expr: str) -> str:
    return f"groups:\n- name: g\n  rules:\n  - alert: {alert}\n    expr: {expr}\n"


_WINDOW_REASON = (
    "The exporter serves `homelab_deploy_reboot_window_active` from the `weekly_reboot_*` "
    "schedule, so the rule file stays plain YAML with no literal hour. The `and on()` on "
    "PromotionStalled drops the alert when the gauge is missing as well as when it reads 1, "
    "so DeployPipelineExporterAbsent must page a missing gauge, as it does for the hold gauge."
)

PROMOTION_STALLED_WAITS_OUT_THE_REBOOT_WINDOW = Census(
    name="promotion-stalled-is-conjoined-with-the-window-gauge-at-zero",
    reason="PromotionStalled stays quiet while the weekly reboot's no-promotion window defers. "
    + _WINDOW_REASON,
    files=lambda: tracked(_DEPLOY_RULES),
    offence=_alert_expr_offence(
        "PromotionStalled", _STALLED_PATTERN, "conjoin the window gauge at zero"
    ),
    red=(
        Subject(_DEPLOY_RULES, _rules_file("PromotionStalled", "stalled > 0")),
        Subject(
            _DEPLOY_RULES,
            _rules_file(
                "PromotionStalled", f"stalled > 0 and on() ({_WINDOW_GAUGE} == 1)"
            ),
        ),
        Subject(_DEPLOY_RULES, _rules_file("Other", "stalled > 0")),
    ),
    green=(
        Subject(
            _DEPLOY_RULES,
            _rules_file(
                "PromotionStalled",
                f'stalled > 0 and on() ({_WINDOW_GAUGE}{{job="x"}} == 0)',
            ),
        ),
    ),
    must_find=frozenset({_DEPLOY_RULES}),
)

EXPORTER_ABSENT_PAGES_A_MISSING_WINDOW_GAUGE = Census(
    name="deploy-pipeline-absent-alert-pages-a-missing-window-gauge",
    reason=_WINDOW_REASON,
    files=lambda: tracked(_DEPLOY_RULES),
    offence=_alert_expr_offence(
        "DeployPipelineExporterAbsent", _ABSENT_PATTERN, "page a missing window gauge"
    ),
    red=(
        Subject(
            _DEPLOY_RULES,
            _rules_file("DeployPipelineExporterAbsent", "absent(homelab_deploy_hold)"),
        ),
        Subject(
            _DEPLOY_RULES,
            _rules_file("DeployPipelineExporterAbsent", f"{_WINDOW_GAUGE} == 0"),
        ),
    ),
    green=(
        Subject(
            _DEPLOY_RULES,
            _rules_file("DeployPipelineExporterAbsent", f"absent({_WINDOW_GAUGE})"),
        ),
    ),
    must_find=frozenset({_DEPLOY_RULES}),
)

_KSM_JOB = "kube-state-metrics"
_UP_SELECTOR = re.compile(r'^up\{job(=~|=)"([^"]+)"\} == 0$')


def _pagers_for_the_ksm_scrape() -> set[str]:
    [group] = yaml_fast.safe_load(_real(_SCRAPE_RULES))["groups"]
    return {
        rule["alert"]
        for rule in group["rules"]
        if (match := _UP_SELECTOR.match(str(rule["expr"]).strip()))
        and _KSM_JOB in match.group(2).split("|")
    }


def _flux_comment_offence(subject: Subject) -> list[str]:
    lines = subject.text.splitlines()
    at = [i for i, line in enumerate(lines) if line.strip() == "- alert: FluxNotReady"]
    if len(at) != 1:
        return [f"{len(at)} FluxNotReady rules, want 1"]
    block: list[str] = []
    for line in reversed(lines[: at[0]]):
        if not line.strip().startswith("#"):
            break
        block.insert(0, line.strip().lstrip("#").strip())
    if not block:
        return ["no comment above FluxNotReady"]
    pagers = _pagers_for_the_ksm_scrape()
    if pagers != {"ScrapeTargetDown"}:
        return [f"{_SCRAPE_RULES} pages the {_KSM_JOB} scrape with {sorted(pagers)}"]
    comment = " ".join(block)
    if not any(name in comment for name in pagers):
        return [f"the comment names none of {sorted(pagers)}: {comment!r}"]
    return []


FLUX_NOT_READY_COMMENT_NAMES_THE_SCRAPE_PAGER = Census(
    name="flux-not-ready-comment-names-the-kube-state-metrics-pager",
    reason=(
        '`FluxNotReady`\'s `absent()` arm is conjoined with `up{job="kube-state-metrics"} == '
        "1` because another rule already pages that scrape's failure. `ScrapeTargetDown` is "
        "that pager since Kuma's Cluster Scrape Targets tile retired. The oracle for the name "
        "is `scrape-targets.yaml` itself: the comment above the rule must name an alert whose "
        "rule selects the kube-state-metrics job."
    ),
    files=lambda: tracked(_FLUX_RULES),
    offence=_flux_comment_offence,
    red=(
        Subject(_FLUX_RULES, "# Paged elsewhere.\n  - alert: FluxNotReady\n"),
        Subject(_FLUX_RULES, "key: 1\n  - alert: FluxNotReady\n"),
        Subject(_FLUX_RULES, "# ScrapeTargetDown pages it.\n"),
        Subject(_FLUX_RULES, _real(_FLUX_RULES).replace("ScrapeTargetDown", "Gone")),
    ),
    green=(
        Subject(_FLUX_RULES, "# ScrapeTargetDown pages it.\n  - alert: FluxNotReady\n"),
        Subject(_FLUX_RULES),
    ),
    must_find=frozenset({_FLUX_RULES}),
)
