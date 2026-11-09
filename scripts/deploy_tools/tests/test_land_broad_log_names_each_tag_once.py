"""A broad landing's log names each changed-set tag at most once (#7894).

A landing whose changed set names some seventy tags printed the list three times: the
changed-set line, the health lines and the `VERDICT:` line. The landing runs in agent
sessions, so each repeat costs context. The full list belongs in the verdict file.

Every boundary is a `_land_fakes.py` fake except land-wait, the real `promote_wait.land_wait`
over `_land_wait_fakes.Sim`, and the health gate, the real `detach_notify.gate` over a faked
probe.

Run: uv run pytest scripts/deploy_tools/tests/test_land_broad_log_names_each_tag_once.py
"""

import re
import types
from dataclasses import replace

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes, build_classifier, build_tools
from _land_wait_fakes import M, P, Sim, at, digests
from deploy_tools.deploy_lib import detach_notify
from deploy_tools.land_lib import promotion as land_promotion
from deploy_tools.promote_lib import wait as promote_wait

# Enough tags that any "broad" threshold a fix picks is crossed; the PR's paths name the first.
BROAD = [f"svc{i:02d}" for i in range(24)]
OWN = BROAD[0]
UNHEALTHY = BROAD[5]
SKIPPED = BROAD[7]
VERDICT = re.compile(r"^VERDICT: (\S+)")


def _probe(argv, unhealthy: frozenset[str]):
    """`probe.py health <tag>` as the gate runs it: SKIPPED has no workload."""
    tag = argv[argv.index("health") + 1]
    if tag == SKIPPED:
        return types.SimpleNamespace(
            returncode=0,
            stdout=f"{tag}: the role declares no rollout-checkable workload",
            stderr="",
        )
    if tag in unhealthy:
        return types.SimpleNamespace(
            returncode=1, stdout=f"{tag}: 0/1 ready — rollout incomplete", stderr=""
        )
    return types.SimpleNamespace(
        returncode=0, stdout=f"{tag}: all 1 workload healthy", stderr=""
    )


@pytest.fixture
def broad_landing(monkeypatch, capsys, tmp_path):
    """Land a PR whose changed set names every BROAD tag; (rc, stdout, verdict file text)."""
    import land

    def run(unhealthy: bool = False):
        old = digests(**{t.replace("-", "_"): "1" for t in BROAD})
        new = digests(**{t.replace("-", "_"): "2" for t in BROAD})
        sim = Sim(polls=[at(M, *BROAD)], published={M: new, P: old})
        fakes = Fakes(
            gh_views={
                "files,changedFiles": {
                    "files": [
                        {"path": f"ansible/roles/k8s/{OWN}/templates/deploy.yaml.j2"}
                    ],
                    "changedFiles": 1,
                }
            },
            derived=([OWN], "pr"),
            k8s_tags={MERGE_SHA: set(BROAD)},
        )
        tools, calls = build_tools(fakes)

        def land_wait(sha, services, primary, cap_s, window_grace_s):
            return promote_wait.land_wait(
                sim.reads(), M, services, cap_s=cap_s, window_grace_s=window_grace_s
            )

        sick = frozenset({UNHEALTHY} if unhealthy else ())

        def gate(tags, cwd=None):
            return detach_notify.gate(
                tags,
                ansible_ok=True,
                tools=detach_notify.NotifyTools(
                    run=lambda argv, **_: _probe(argv, sick),
                    tag_platforms=lambda tag, host_vars: {"k8s"},
                ),
                cwd=cwd,
            )

        tools = replace(tools, flux=replace(tools.flux, land_wait=land_wait), gate=gate)
        verdict_file = tmp_path / "verdict"
        monkeypatch.setenv("LAND_PRIMARY", str(PRIMARY))
        rc = land.main(
            ["--pr", "999", "--verdict-file", str(verdict_file)],
            tools=tools,
            classifier=build_classifier(fakes, calls),
        )
        return rc, capsys.readouterr().out, verdict_file.read_text()

    return run


def _lines_naming(out: str, tag: str) -> list[str]:
    return [line for line in out.splitlines() if re.search(rf"\b{tag}\b", line)]


def test_a_broad_landing_names_each_changed_set_tag_on_at_most_one_line(
    broad_landing,
):
    rc, out, _ = broad_landing()
    assert rc == 0, out
    repeated = {
        tag: lines for tag in BROAD if len(lines := _lines_naming(out, tag)) > 1
    }
    assert not repeated, out


def test_a_broad_verdict_line_counts_the_tags_and_the_verdict_file_lists_them(
    broad_landing,
):
    _, out, verdict_file = broad_landing()
    verdict_line = next(line for line in out.splitlines() if VERDICT.match(line))
    verdict = VERDICT.match(verdict_line)
    assert verdict and verdict[1] == "settled", out
    assert re.search(rf"\b{len(BROAD)}\b", verdict_line), verdict_line
    named = [tag for tag in BROAD if re.search(rf"\b{tag}\b", verdict_line)]
    assert len(named) <= 1, verdict_line
    recorded = VERDICT.match(verdict_file)
    assert recorded and recorded[1] == "settled", verdict_file
    assert all(re.search(rf"\b{tag}\b", verdict_file) for tag in BROAD), verdict_file


def test_health_prints_a_count_line_and_lists_only_the_tags_not_healthy(
    broad_landing,
):
    """Healthy and skipped tags fold into one count line; the unhealthy one is named."""
    rc, out, _ = broad_landing(unhealthy=True)
    assert rc == 1, out
    health = out.split("health verdict", 1)[1].split("VERDICT:", 1)[0]
    healthy = len(BROAD) - 2
    assert any(
        re.search(rf"\b{healthy}\b.*healthy", line)
        and re.search(r"\b1\b.*skipped", line)
        for line in health.splitlines()
    ), health
    assert not _lines_naming(health, SKIPPED), health
    assert "workload healthy" not in health, health
    assert any(
        "rollout incomplete" in line for line in _lines_naming(health, UNHEALTHY)
    )


def test_the_changed_set_line_names_only_the_tags_beyond_the_prs_own(broad_landing):
    """The PR's paths named OWN, so the line listing the extra tags leaves it to the count."""
    _, out, _ = broad_landing()
    extras = [line for line in out.splitlines() if re.search(rf"\b{BROAD[1]}\b", line)]
    assert extras, out
    assert not [line for line in extras if re.search(rf"\b{OWN}\b", line)], extras
    promotion = out.split("waiting on the promotion", 1)[1].split("== ", 1)[0]
    # The count may be the whole set's or the extras' alone.
    assert re.search(rf"\b({len(BROAD)}|{len(BROAD) - 1})\b", promotion), promotion


def test_a_broad_set_the_prs_paths_named_none_of_prints_only_its_count():
    """A range `changed` refused as broad names no tag, the land7672 shape."""
    line = land_promotion.changed_set_line(BROAD, [], widened=False)
    assert re.search(rf"\b{len(BROAD)}\b", line), line
    assert not [tag for tag in BROAD if re.search(rf"\b{tag}\b", line)], line


def test_a_widened_set_prints_only_its_count_whatever_the_pr_named():
    line = land_promotion.changed_set_line(BROAD, [OWN], widened=True)
    assert re.search(rf"\b{len(BROAD)}\b", line), line
    assert not [tag for tag in BROAD if re.search(rf"\b{tag}\b", line)], line


def test_a_small_set_the_prs_paths_named_none_of_is_listed():
    line = land_promotion.changed_set_line(BROAD[:3], [], widened=False)
    assert all(re.search(rf"\b{tag}\b", line) for tag in BROAD[:3]), line
