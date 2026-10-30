"""A scrape target that flaps without holding down for ScrapeTargetDown's `for:` still pages (#6626).

`ScrapeTargetDown` holds each series at `up == 0` for 10 minutes, so a target that fails for 1
to 4 minutes at a time never fires it. Kuma's Cluster Scrape Targets tile paged that case
because it kept one streak for the whole check, and A3's Cutover-a deletes the tile. The flap
arm is a rule in `files/rules/scrape-targets.yaml` that reads a range of `up`, such as
`avg_over_time(up[30m]) < 0.7`, with a `for:` hold.

This module finds the arm by its range selector over `up`, not by name or function, and holds:
it carries the per-role `service` literals `ScrapeTargetDown` carries, and its promtool test
has a case that fires on an alternating series ScrapeTargetDown misses and a case that stays
quiet on a single rollout gap. The pytest shards install no promtool, so this reads the test
file's cases; the validate-alert-rules hook runs them.

Run: uv run pytest ansible/tests/services/test_scrape_target_flap_rule.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast

TESTS = K8S_ROLES / "observability" / "tests" / "rules" / "scrape-targets.test.yaml"
RULES = K8S_ROLES / "observability" / "files" / "rules" / "scrape-targets.yaml"
# `up` followed by an optional label matcher and then a range: `up[30m]`, `up{job="x"}[30m]`.
UP_RANGE = re.compile(r"\bup(\{[^}]*\})?\[")
JOB_MATCHER = re.compile(r'job(=~|=|!~)"([^"]+)"')
DURATION = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")
SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}
EXPANDING = re.compile(r"^(-?[\d.e+]+)([+-][\d.e+]+)?x(\d+)$")


def rules() -> list[dict]:
    [group] = yaml_fast.safe_load(RULES.read_text())["groups"]
    return group["rules"]


def flap_rules() -> list[dict]:
    """The alerting rules whose expression reads a range of `up`."""
    return [r for r in rules() if "alert" in r and UP_RANGE.search(str(r["expr"]))]


def seconds(duration: str) -> float:
    """A Prometheus duration such as `10m` or `1h30m`, in seconds."""
    return sum(int(n) * SECONDS[unit] for n, unit in DURATION.findall(str(duration)))


def expand(values: str) -> list[float | None]:
    """A promtool `values` string as one sample per interval, `None` for `_` and `stale`."""
    out: list[float | None] = []
    for token in str(values).split():
        if token in ("_", "stale"):
            out.append(None)
            continue
        if token.startswith("_x"):
            out.extend([None] * (int(token[2:]) + 1))
            continue
        found = EXPANDING.match(token)
        if not found:
            out.append(float(token))
            continue
        start, step, count = found.groups()
        delta = float(step) if step else 0.0
        out.extend(float(start) + delta * i for i in range(int(count) + 1))
    return out


def zero_runs(samples: list[float | None]) -> list[int]:
    """The lengths of the runs of consecutive `0` samples; `_` and `stale` end a run."""
    runs: list[int] = []
    current = 0
    for sample in samples:
        if sample == 0:
            current += 1
            continue
        if current:
            runs.append(current)
        current = 0
    if current:
        runs.append(current)
    return runs


def up_series_runs(case: dict) -> list[list[int]]:
    """Each `up` input series' zero runs, for one promtool test case."""
    return [
        zero_runs(expand(s["values"]))
        for s in case.get("input_series") or []
        if re.match(r"^up(\{|$)", s["series"])
    ]


def flap_checks(case: dict, names: set[str]) -> list[dict]:
    return [t for t in case.get("alert_rule_test") or [] if t["alertname"] in names]


def test_scrape_targets_carry_a_flap_arm_with_a_hold():
    found = flap_rules()
    assert found, f"no alerting rule in {RULES.name} reads a range of up"
    assert all(seconds(r.get("for", "0s")) > 0 for r in found), [
        (r["alert"], r.get("for")) for r in found
    ]


def test_the_flap_arm_carries_scrape_target_downs_service_per_job():
    found = flap_rules()
    assert found, f"no alerting rule in {RULES.name} reads a range of up"
    down = [r for r in rules() if r.get("alert") == "ScrapeTargetDown"]
    assert {r["labels"]["service"] for r in found} == {
        r["labels"]["service"] for r in down
    }

    def by_job(rule_list: list[dict]) -> dict[str, str]:
        mapped: dict[str, str] = {}
        for rule in rule_list:
            for op, jobs in JOB_MATCHER.findall(str(rule["expr"])):
                if op != "!~":
                    for job in jobs.split("|"):
                        mapped[job] = rule["labels"]["service"]
        return mapped

    assert by_job(found) == by_job(down)


def test_a_promtool_case_fires_the_flap_arm_on_a_flap_scrape_target_down_misses():
    found = flap_rules()
    assert found, f"no alerting rule in {RULES.name} reads a range of up"
    names = {r["alert"] for r in found}
    hold = min(
        seconds(r["for"]) for r in rules() if r.get("alert") == "ScrapeTargetDown"
    )
    cases = yaml_fast.safe_load(TESTS.read_text())["tests"]
    firing = [
        case
        for case in cases
        if any(
            len(runs) >= 2 and max(runs) * seconds(case["interval"]) < hold
            for runs in up_series_runs(case)
        )
        and any(t.get("exp_alerts") for t in flap_checks(case, names))
    ]
    assert firing, (
        f"no case in {TESTS.name} feeds an up series with two or more zero runs, each "
        f"shorter than ScrapeTargetDown's hold, and expects {sorted(names)} to fire"
    )


def test_a_promtool_case_keeps_the_flap_arm_quiet_on_one_rollout_gap():
    found = flap_rules()
    assert found, f"no alerting rule in {RULES.name} reads a range of up"
    names = {r["alert"] for r in found}
    cases = yaml_fast.safe_load(TESTS.read_text())["tests"]

    def quiet_after_gap(case: dict) -> bool:
        runs = up_series_runs(case)
        if not runs or any(len(r) > 1 for r in runs) or not any(runs):
            return False
        first_zero = min(
            expand(s["values"]).index(0)
            for s in case["input_series"]
            if re.match(r"^up(\{|$)", s["series"]) and 0 in expand(s["values"])
        )
        gap_start = first_zero * seconds(case["interval"])
        return any(
            not t.get("exp_alerts") and seconds(t["eval_time"]) > gap_start
            for t in flap_checks(case, names)
        )

    assert any(quiet_after_gap(c) for c in cases), (
        f"no case in {TESTS.name} feeds an up series with exactly one zero run and expects "
        f"{sorted(names)} quiet after the gap starts"
    )


def test_a_promtool_case_keeps_the_flap_arm_quiet_after_a_gap_as_long_as_the_hold_recovers():
    """One zero run that ends, at least ScrapeTargetDown's hold long, with the flap arm quiet
    10 minutes or more after it ends. That is where a single gap's recovery tail still holds
    the 30-minute average under 0.7, so a guard that ends at the gap cannot see it."""
    names = {r["alert"] for r in flap_rules()}
    assert names, f"no alerting rule in {RULES.name} reads a range of up"
    hold = min(
        seconds(r["for"]) for r in rules() if r.get("alert") == "ScrapeTargetDown"
    )
    cases = yaml_fast.safe_load(TESTS.read_text())["tests"]

    def quiet_in_recovery_tail(case: dict) -> bool:
        step = seconds(case["interval"])
        ups = [
            expand(s["values"])
            for s in case.get("input_series") or []
            if re.match(r"^up(\{|$)", s["series"])
        ]
        if len(ups) != 1 or len(zero_runs(ups[0])) != 1:
            return False
        samples = ups[0]
        start = samples.index(0)
        end = start + zero_runs(samples)[0]
        if end >= len(samples) or samples[end] != 1 or (end - start) * step < hold:
            return False
        return any(
            not t.get("exp_alerts") and seconds(t["eval_time"]) >= end * step + 600
            for t in flap_checks(case, names)
        )

    assert any(quiet_in_recovery_tail(c) for c in cases), (
        f"no case in {TESTS.name} feeds one zero run of at least ScrapeTargetDown's hold "
        f"that recovers, and expects {sorted(names)} quiet 10 minutes after it recovers"
    )
