"""No test docstring describes Uptime Kuma as something that still pages (#7316).

M6 deleted Kuma, and `_kuma_census_row.py` leaves test directories outside its selector, so no
census row reads these docstrings. A sentence may still name Kuma as history. This is one test
rather than a `Census` row because a row's red and green cases are separate tests.
"""

import ast
import re

from _row_table import tracked
from lib.repo_paths import REPO

# Capitalised on purpose: the prose noun, not `kuma-check` or a `m6-retire-kuma.md` citation.
LIVE_KUMA = re.compile(
    r"\bKuma's [^.;:]*\balert channel\b"
    r"|\bKuma(?: already)? (?:proves|truncates|reads)\b"
    r"|\b(?:pushes|pushed to|folds \w+ into)\b(?! nothing)[^.;:]*\bKuma\b"
    r"|\breach Kuma\b"
    r"|\binvisible to Kuma\b"
    r"|(?<!\bno )\bKuma(?: and \w+)? (?:tiles?|monitors?|prober|verdict|crons?)\b"
    r"|\bKuma's (?:[\w-]+ ){0,4}tiles?\b"
)
HISTORY = re.compile(
    r"\b(?:until|since|once(?! the)|retired?|removed|dropped|deleted|replaced|paged|had"
    r"|reproduce|whether)\b",
    re.I,
)


def describes_kuma_as_live(sentence: str) -> bool:
    return bool(LIVE_KUMA.search(sentence)) and not HISTORY.search(sentence)


def docstrings(rel: str) -> list[tuple[str, str]]:
    """Each (owner, docstring) in one Python file, the module's owner being `<module>`."""
    found = []
    for node in ast.walk(ast.parse((REPO / rel).read_text())):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            doc = ast.get_docstring(node)
            if doc:
                found.append((getattr(node, "name", "<module>"), " ".join(doc.split())))
    return found


def test_no_test_docstring_describes_kuma_as_a_live_pager():
    for red in (
        "`smtp_notify_app_password` is Uptime Kuma's second alert channel.",
        "465 is the transport Kuma already proves against Gmail",
        "No `origin`, so Kuma's Cluster Scrape Targets tile pages a failed scrape.",
        "Every on-prem heartbeat here pushes to Uptime Kuma.",
    ):
        assert describes_kuma_as_live(red), red
    for green in (
        "Kuma's Cluster Scrape Targets tile paged that case.",
        "M6 batch B2's Cutover-2 removed the Kuma push.",
        "disk-health.sh pushes nothing to Kuma.",
        "A rule that regains `shadow` pages nobody while no Kuma tile pages in its place.",
        "The shared kuma-check timer pair reruns a host check.",
    ):
        assert not describes_kuma_as_live(green), green

    files = [
        rel
        for rel in tracked("ansible/tests/**", "scripts/**/tests/**")
        if rel.endswith(".py")
    ]
    assert "ansible/tests/services/test_smtp_wiring.py" in files
    assert "scripts/validate/tests/test_backup_health_shim.py" in files

    offenders = [
        f"{rel}::{owner}: {sentence}"
        for rel in files
        for owner, doc in docstrings(rel)
        for sentence in re.split(r"(?<=[.;:])\s+", doc)
        if describes_kuma_as_live(sentence)
    ]
    assert not offenders, (
        "M6 deleted Kuma, so say what pages instead, or put the sentence in the past tense:\n"
        + "\n".join(offenders)
    )


# No exemption word: "paged" and "pushes nothing to" stay out by their own shape, so a present-tense
# claim cannot pass on a "since" or "once" elsewhere in the sentence.
STATES_KUMA_PAGES = re.compile(
    r"(?<!\bno )\bKuma(?:'s)?(?: (?!as\b)[\w-]+){0,4} "
    r"(?:pages?|alerts?|notifies|goes|proves|truncates|reads|watches)\b"
    r"|\bKuma is (?!gone\b)"
    r"|\b(?:through|via|pushes to|pushes into) (?:prod's )?(?:Uptime )?Kuma\b"
)


def test_a_plain_statement_that_kuma_pages_is_flagged():
    for sentence in (
        "This pages through Uptime Kuma.",
        "Uptime Kuma pages the operator on a failed scrape.",
        "Kuma alerts on a stale heartbeat.",
        "Kuma is the second alert channel.",
        "The Kuma push goes DOWN for any overdue secret.",
        "Kuma's Cluster Scrape Targets tile pages a failed scrape once it stops.",
        "Kuma's tile pages it since the scrape failed.",
        "Every heartbeat pushes to Kuma, which had a grace period.",
    ):
        assert STATES_KUMA_PAGES.search(sentence), sentence


def test_kuma_named_as_history_is_clean():
    for sentence in (
        "Kuma's Cluster Scrape Targets tile paged that case.",
        "M6 batch B2's Cutover-2 removed the Kuma push.",
        "disk-health.sh pushes nothing to Kuma.",
        "A rule that regains `shadow` pages nobody while no Kuma tile pages in its place.",
        "A comment that cites the plan stays true after Kuma is gone.",
        "`monitor_bridge_ha_token` carries the prefix for Kuma history but is an HA API token.",
    ):
        assert not STATES_KUMA_PAGES.search(sentence), sentence


def test_no_test_docstring_states_that_kuma_pages():
    files = [
        rel
        for rel in tracked("ansible/tests/**", "scripts/**/tests/**")
        if rel.endswith(".py")
    ]
    assert "ansible/tests/services/test_smtp_wiring.py" in files
    offenders = [
        f"{rel}::{owner}: {sentence}"
        for rel in files
        for owner, doc in docstrings(rel)
        for sentence in re.split(r"(?<=[.;:])\s+", doc)
        if STATES_KUMA_PAGES.search(sentence)
    ]
    assert not offenders, "M6 deleted Kuma:\n" + "\n".join(offenders)
