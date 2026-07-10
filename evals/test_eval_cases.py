"""Schema validation for the homelab eval cases consumed by the chezmoi engine.

These are cheap, offline guards (no `claude` calls): they only assert each case
file is well-formed so a typo can't make a case silently not run. The paid LLM
eval itself is run manually — see evals/README.md.
"""

import json
import re
from pathlib import Path

CASES_DIR = Path(__file__).parent / "cases"
REQUIRED = ("id", "agent", "input", "assert", "rubric", "k", "threshold")
_THRESHOLD_RE = re.compile(r"^(all|rate>=\d+/\d+)$")


def validate_case(obj: dict) -> list[str]:
    problems: list[str] = []
    for field in REQUIRED:
        if field not in obj:
            problems.append(f"missing field: {field}")
    if "assert" in obj:
        a = obj["assert"]
        if (
            not isinstance(a, dict)
            or "must_match" not in a
            or "must_not_match" not in a
        ):
            problems.append(
                "assert must be an object with must_match and must_not_match arrays"
            )
        else:
            for key in ("must_match", "must_not_match"):
                value = a.get(key, [])
                if not isinstance(value, list):
                    problems.append(f"{key} must be an array")
                    continue
                for pat in value:
                    if not isinstance(pat, str):
                        problems.append(f"{key} pattern is not a string: {pat!r}")
                        continue
                    try:
                        re.compile(pat)
                    except re.error as e:
                        problems.append(f"{key} has invalid regex {pat!r}: {e}")
    if "threshold" in obj and not _THRESHOLD_RE.match(str(obj["threshold"])):
        problems.append(
            f"bad threshold: {obj['threshold']!r} (want 'all' or 'rate>=X/Y')"
        )
    if (
        "id" in obj
        and "agent" in obj
        and not str(obj["id"]).startswith(f"{obj['agent']}/")
    ):
        problems.append(f"id {obj['id']!r} must start with '{obj['agent']}/'")
    if obj.get("mode") not in (None, "live"):
        problems.append(f"unknown mode: {obj['mode']!r}")
    return problems


def _all_case_files() -> list[Path]:
    return sorted(CASES_DIR.rglob("*.json"))


def test_validate_case_accepts_a_good_case():
    good = {
        "id": "security-review/001",
        "agent": "security-review",
        "input": "x",
        "assert": {"must_match": ["High"], "must_not_match": []},
        "rubric": "r",
        "k": 3,
        "threshold": "rate>=2/3",
    }
    assert validate_case(good) == []


def test_validate_case_flags_missing_field_and_bad_threshold_and_regex():
    bad = {
        "id": "x/1",
        "agent": "x",
        "input": "i",
        "assert": {"must_match": ["("], "must_not_match": []},
        "rubric": "r",
        "k": 3,
        "threshold": "most",
    }
    problems = validate_case(bad)
    assert any("bad threshold" in p for p in problems)
    assert any("invalid regex" in p for p in problems)


def test_validate_case_flags_must_match_not_a_list():
    bad = {
        "id": "x/1",
        "agent": "x",
        "input": "i",
        "assert": {"must_match": "High", "must_not_match": []},
        "rubric": "r",
        "k": 3,
        "threshold": "all",
    }
    problems = validate_case(bad)
    assert problems


def test_validate_case_flags_non_string_pattern_element():
    bad = {
        "id": "x/1",
        "agent": "x",
        "input": "i",
        "assert": {"must_match": [123], "must_not_match": []},
        "rubric": "r",
        "k": 3,
        "threshold": "all",
    }
    problems = validate_case(bad)
    assert problems


def test_validate_case_flags_id_not_prefixed_with_agent():
    bad = {
        "id": "wrong-prefix/1",
        "agent": "x",
        "input": "i",
        "assert": {"must_match": [], "must_not_match": []},
        "rubric": "r",
        "k": 3,
        "threshold": "all",
    }
    problems = validate_case(bad)
    assert any("must start with" in p for p in problems)


def test_validate_case_flags_unknown_mode():
    bad = {
        "id": "x/1",
        "agent": "x",
        "input": "i",
        "assert": {"must_match": [], "must_not_match": []},
        "rubric": "r",
        "k": 3,
        "threshold": "all",
        "mode": "bogus",
    }
    problems = validate_case(bad)
    assert any("unknown mode" in p for p in problems)


def test_all_case_files_valid():
    files = _all_case_files()
    assert files, "no case files found under evals/cases/"
    for f in files:
        obj = json.loads(f.read_text())
        problems = validate_case(obj)
        assert not problems, f"{f}: {problems}"
