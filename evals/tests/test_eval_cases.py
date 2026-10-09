"""Schema validation for the homelab eval cases consumed by the chezmoi engine.

These are cheap, offline guards (no `claude` calls): they only assert each case
file is well-formed so a typo can't make a case silently not run. The paid LLM
eval itself is run manually — see evals/README.md.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from lib.repo_paths import REPO

CASES_DIR = Path(__file__).parent.parent / "cases"
PI_HOST_VARS = REPO / "ansible/inventory/host_vars/daniel-pi.yml"
# A Compose role path, cited bare or under `ansible/`. The role-name class excludes `*`, so the
# `roles/containers/**` glob a Renovate ruling quotes is not read as a role.
_CONTAINER_ROLE_RE = re.compile(
    r"(?<![\w-])(?:ansible/)?roles/containers/([a-z0-9][a-z0-9_-]*)"
)
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


def _strings(value: object) -> Iterator[str]:
    """Every string in a decoded case, at any depth, so a path in a rubric counts too."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def cited_container_roles(obj: dict) -> set[str]:
    return {m for text in _strings(obj) for m in _CONTAINER_ROLE_RE.findall(text)}


def live_pi_roles() -> set[str]:
    """The Compose roles a case may cite: daniel-pi's containers_list, plus the shared `common`."""
    entries = yaml.safe_load(PI_HOST_VARS.read_text())["containers_list"]
    return {entry["name"] for entry in entries} | {"common"}


def retired_container_roles(obj: dict) -> set[str]:
    """README rule 1: a cited `roles/containers/<x>` must be a live Pi role.

    A fixture for a service that does not exist yet belongs under `roles/k8s/`, marked
    `(a new role, not yet merged)`, because no new service lands on Docker.
    """
    return cited_container_roles(obj) - live_pi_roles()


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


def test_retired_container_role_is_flagged():
    case = {
        "input": "# ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
        "rubric": "see $HOME/server/ansible/roles/containers/ledger/tasks/main.yml",
    }
    assert retired_container_roles(case) == {"dozzle", "ledger"}


def test_live_pi_role_and_renovate_glob_are_not_flagged():
    case = {
        "input": "ansible/roles/containers/wg-easy/tasks/main.yml",
        "rubric": "Renovate must not track `roles/containers/**`.",
    }
    assert retired_container_roles(case) == set()


def test_no_case_cites_a_retired_container_role():
    cited: dict[Path, set[str]] = {
        f: cited_container_roles(json.loads(f.read_text())) for f in _all_case_files()
    }
    # A scan that matches nothing would pass vacuously; skeptic/001 quotes the live wg-easy role.
    skeptic = CASES_DIR / "skeptic/001-refuted-with-evidence.json"
    assert "wg-easy" in cited[skeptic], (
        "the role-path scan no longer finds wg-easy in skeptic/001"
    )
    live = live_pi_roles()
    stale = {
        str(f.relative_to(CASES_DIR)): sorted(roles - live)
        for f, roles in cited.items()
        if roles - live
    }
    assert not stale, (
        f"cases cite roles/containers/ roles that are not live on daniel-pi ({sorted(live)}): {stale}. "
        "Rebuild the case from a live file, or move a new-service fixture under roles/k8s/ "
        "marked '(a new role, not yet merged)' (evals/README.md, rule 1)."
    )
