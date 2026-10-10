"""The red phase made reachable (#3950): `open` labels a suite-covered finding `red-green`, and
a review record says why a batch ran no red phase.

Run: uv run pytest scripts/dev/tests/test_findings_red_green.py
"""

from _findings_fakes import Fakes
from _review_fakes import PR, _pipeline, _report
from fanout_lib.red_gate import red_skip_reason

from dev import findings
from dev.findings_lib.red_green import RED_GREEN_LABEL, red_green_eligible
from dev.findings_lib.tracked_paths import resolve_fragments

COVERED = [
    "scripts/dev/findings.py",
    "ansible/filter_plugins/toposort.py",
    ".claude/hooks/fanout-stop.py",
    "ansible/roles/k8s/monitor-bridge/files/registry.py",
    "ansible/roles/k8s/home-assistant/files/custom_templates/fan.jinja",
]


def test_findings_citing_only_suite_covered_code_are_eligible():
    assert red_green_eligible(COVERED)


def test_a_doc_a_template_or_no_citation_is_not_eligible():
    assert not red_green_eligible([*COVERED, "docs/landing.md"])
    assert not red_green_eligible(
        ["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"]
    )
    # A role with no `tests/` of its own: nothing would run a red test against its files.
    assert not red_green_eligible(["ansible/roles/setup/nosuchrole/files/x.py"])
    assert not red_green_eligible([])


def _open(tmp_path, make_tools, body_text, *extra):
    body = tmp_path / "b.md"
    body.write_text(body_text)
    tools, calls = make_tools(Fakes(issues=[]))
    argv = ["open", "--title", "T", "--body-file", str(body), "--severity", "low"]
    assert findings.main([*argv, "--kind", "gap", *extra], tools) == 0
    return calls


def test_open_labels_a_suite_covered_finding_and_creates_the_label(
    tmp_path, make_tools
):
    calls = _open(tmp_path, make_tools, "`scripts/dev/findings.py` drops the row.")
    create = next(c for c in calls.gh if c[:2] == ["issue", "create"])
    assert RED_GREEN_LABEL in create
    assert ["label", "create", RED_GREEN_LABEL] in [c[:3] for c in calls.gh]


def test_open_leaves_a_finding_citing_a_doc_unlabelled(tmp_path, make_tools):
    calls = _open(
        tmp_path, make_tools, "`scripts/dev/findings.py` and `docs/landing.md`."
    )
    create = next(c for c in calls.gh if c[:2] == ["issue", "create"])
    assert RED_GREEN_LABEL not in create


def test_a_fragment_resolves_to_the_one_tracked_file_that_ends_with_it():
    """#4232: #4116 cited `fanout_lib/clean.py`, meaning `scripts/dev/fanout_lib/clean.py`."""
    tracked = {"scripts/dev/fanout_lib/clean.py", "a/lib/x.py", "b/lib/x.py"}
    cited = ["fanout_lib/clean.py", "lib/x.py", "docs/gone.md"]
    assert resolve_fragments(cited, tracked) == [
        "scripts/dev/fanout_lib/clean.py",
        "lib/x.py",
        "docs/gone.md",
    ]


def test_open_labels_a_finding_that_cites_suite_covered_code_by_a_fragment(
    tmp_path, make_tools
):
    calls = _open(tmp_path, make_tools, "`fanout_lib/clean.py:clean_one` drops it.")
    create = next(c for c in calls.gh if c[:2] == ["issue", "create"])
    assert RED_GREEN_LABEL in create


def test_red_skip_reason_names_each_way_a_batch_misses_the_phase():
    assert red_skip_reason([[RED_GREEN_LABEL]], server=True) == ""
    assert red_skip_reason([["claude"]], server=True) == "no-label"
    assert red_skip_reason([[RED_GREEN_LABEL], ["claude"]], True) == "unlabelled-issue"
    assert red_skip_reason([[RED_GREEN_LABEL]], server=False) == "other-repo"


def test_the_review_record_says_why_the_batch_ran_no_red_phase(tmp_path):
    reports = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    pipeline, run = _pipeline(tmp_path, reports, host="daniel-server")
    run.labels = {"labels": [{"name": "claude"}]}
    pipeline.run_all()
    assert pipeline.record.red_skipped == "no-label"
