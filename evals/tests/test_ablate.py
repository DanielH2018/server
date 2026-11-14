import os
import re
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ablate import (
    REPO,
    SWEEP_WEEKDAY,
    doc_without,
    main,
    split_sections,
    with_doc,
)

DOC = """# Title
Intro line.

## Alpha
Alpha body.
### Alpha detail
More alpha.

## Beta
```bash
## not a heading, inside a fence
```
Beta body.
"""

AGENT = "---\nname: skeptic\ndescription: d\n---\nYou verify findings.\n"

CASES = [
    {"id": "a/1", "agent": "x", "threshold": "all"},
    {"id": "a/2", "agent": "x", "threshold": "all"},
]


def test_sections_split_on_level_two_headings_outside_fences():
    preamble, sections = split_sections(DOC)
    assert preamble == "# Title\nIntro line.\n\n"
    assert [h for h, _ in sections] == ["Alpha", "Beta"]
    assert "### Alpha detail" in sections[0][1]
    assert "## not a heading" in sections[1][1]


def test_an_ablated_prompt_differs_from_the_baseline_only_by_the_section():
    alpha = split_sections(DOC)[1][0][1]
    baseline = with_doc(AGENT, "CLAUDE.md", DOC)
    ablated = with_doc(AGENT, "CLAUDE.md", doc_without(DOC, "Alpha"))
    assert ablated != baseline
    assert baseline.replace(alpha, "", 1) == ablated
    assert baseline.startswith("---\nname: skeptic\n")


def test_the_whole_doc_arm_is_the_agent_file_unchanged():
    assert with_doc(AGENT, "CLAUDE.md", None) == AGENT


def test_sweep_weekday_matches_the_timer():
    timer = (
        REPO / "hosts/components/eval-sweep/units/homelab-eval-sweep.timer"
    ).read_text()
    m = re.search(r"^OnCalendar=(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", timer, re.M)
    assert m, "the eval sweep timer no longer names a weekday"
    # Python's weekday() counts Monday as 0.
    timer_weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(m.group(1))
    assert timer_weekday == SWEEP_WEEKDAY


def test_rank_counts_a_row_with_a_form_and_a_size_in_its_tail(tmp_path, capsys):
    log = tmp_path / "instructions.log"
    log.write_text(
        "t [s] bash_path_match Project a/CLAUDE.md trigger=a/x form=head bytes=7\n"
    )
    assert main(["rank", "--log", str(log)]) == 0
    assert capsys.readouterr().out.split() == ["1", "a/CLAUDE.md"]
