"""The comment above `FluxNotReady` names the rule that pages a failed kube-state-metrics scrape.

`FluxNotReady`'s `absent()` arm is conjoined with `up{job="kube-state-metrics"} == 1` because
another rule already pages that scrape's failure (M1 convention 5). M6 A3's Cutover-a1 (#6651)
retired Kuma's Cluster Scrape Targets tile, so `ScrapeTargetDown` is that pager (#6661). The
oracle for the name is `scrape-targets.yaml` itself: the comment must name an alert whose rule
selects the kube-state-metrics job.

Run: uv run pytest ansible/tests/services/test_flux_not_ready_comment_pager.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules"
JOB = "kube-state-metrics"
SELECTOR = re.compile(r'^up\{job(=~|=)"([^"]+)"\} == 0$')


def comment_above(alert: str) -> str:
    """The contiguous `#` comment lines directly above `- alert: <alert>` in flux.yaml."""
    lines = (RULES / "flux.yaml").read_text().splitlines()
    [index] = [i for i, line in enumerate(lines) if line.strip() == f"- alert: {alert}"]
    block: list[str] = []
    for line in reversed(lines[:index]):
        if not line.strip().startswith("#"):
            break
        block.insert(0, line.strip().lstrip("#").strip())
    assert block, f"no comment above {alert}"
    return " ".join(block)


def pager_for(job: str) -> set[str]:
    """The alert names in scrape-targets.yaml whose positive selector matches `job`."""
    [group] = yaml_fast.safe_load((RULES / "scrape-targets.yaml").read_text())["groups"]
    names = set()
    for rule in group["rules"]:
        match = SELECTOR.match(str(rule["expr"]).strip())
        if match and job in match.group(2).split("|"):
            names.add(rule["alert"])
    return names


def test_comment_names_the_rule_that_pages_the_kube_state_metrics_scrape():
    pagers = pager_for(JOB)
    assert pagers == {"ScrapeTargetDown"}, pagers
    comment = comment_above("FluxNotReady")
    named = {name for name in pagers if name in comment}
    assert named, (
        f"the comment above FluxNotReady names none of {sorted(pagers)}, the rule that pages "
        f"up{{job={JOB!r}}} == 0: {comment!r}"
    )


def test_comment_no_longer_names_the_retired_kuma_tile_as_the_pager():
    comment = comment_above("FluxNotReady")
    assert "Cluster Scrape Targets" not in comment, (
        f"the comment above FluxNotReady still names Kuma's retired Cluster Scrape Targets "
        f"tile: {comment!r}"
    )
