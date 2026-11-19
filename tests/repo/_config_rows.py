"""The census rows over the repo's own config, gathered into `CONFIG_ROWS`.

The review skill, its agents and the karakeep time-tagger fork are defined here; the prek and
gitleaks rows and the alert-rule rows are in the two modules imported below.

Each row replaces a test file of its own. A row's `reason` keeps what that file's docstring
said a reader needs before changing the rule. `test_census_rows_text.py` collects `CONFIG_ROWS`.
"""

import hashlib
import re
from pathlib import Path

from _alert_rule_rows import (
    EXPORTER_ABSENT_PAGES_A_MISSING_WINDOW_GAUGE,
    FLUX_NOT_READY_COMMENT_NAMES_THE_SCRAPE_PAGER,
    PROMOTION_STALLED_WAITS_OUT_THE_REBOOT_WINDOW,
)
from _helpers import REPO
from _prek_gitleaks_rows import (
    GITLEAKS_MANIFEST_DIGEST_ALLOWLIST,
    GITLEAKS_SECRET_KEY_NAME_ALLOWLIST,
    PREK_DEFAULTS_TO_THE_COMMIT_STAGE,
    PREK_REMOTE_HOOKS_PIN_THE_COMMIT_STAGE,
    PREK_UV_ENTRIES_ARE_FROZEN,
)
from _row_table import Census, Subject, tracked
from dev.findings_lib.issue_model import DOMAINS


def _real(rel: str) -> str:
    return (REPO / rel).read_text(errors="replace")


# ── The homelab-review skill still instructs the fix-skeptic pass ─────────────────────

_SKILL = ".claude/skills/homelab-review/SKILL.md"
_SKEPTIC_MARKERS = ("fix-skeptic", "skeptic", "SAFE", "LAUNDERS", "UNSAFE")
_MIN_SKILL_CHARS = 1000


def _skeptic_offence(subject: Subject) -> list[str]:
    problems = [f"missing {m!r}" for m in _SKEPTIC_MARKERS if m not in subject.text]
    if len(subject.text) <= _MIN_SKILL_CHARS:
        problems.append(f"{len(subject.text)} characters, floor is {_MIN_SKILL_CHARS}")
    return problems


FIX_SKEPTIC_PASS_INSTRUCTED = Census(
    name="fix-skeptic-pass-is-still-instructed",
    reason=(
        "Recurring-failure class 1 (docs/failure-classes.md) is that findings get an adversarial "
        "pass and fixes get none. Its remedy is a review procedure, a reviewer dispatching a "
        "`skeptic` agent against every recommended remediation per "
        "`.claude/skills/homelab-review/SKILL.md` step 7, and no test can verify that a session "
        "ran it. This proxy-citation guard proves only that the skill still SAYS to run the "
        "pass. Removing the instruction reopens exactly the gap the class names."
    ),
    files=lambda: tracked(_SKILL),
    offence=_skeptic_offence,
    red=(
        Subject(_SKILL, _real(_SKILL).replace("fix-skeptic", "").replace("SAFE", "")),
        Subject(_SKILL, "fix-skeptic skeptic SAFE LAUNDERS UNSAFE"),
    ),
    green=(Subject(_SKILL),),
    must_find=frozenset({_SKILL}),
)


# ── Reviewer agents and the shared accepted-designs file ──────────────────────────────

_DESIGNS = ".claude/skills/homelab-review/accepted-designs.md"
_REGISTER = "docs/reference/backlog.md"
# The domain each pointed agent reviews, keyed by its file name. A new reviewer agent that
# carries a domain section joins here so the pointer check covers it.
_POINTED_AGENTS = {
    "homelab-container-reviewer.md": "container",
    "homelab-cicd-reviewer.md": "cicd",
    "homelab-backup-observability-reviewer.md": "backup-observability",
}
_DOMAIN_SECTIONS = "## container\n## cicd\n## backup-observability\n"


def _sections(text: str) -> list[str]:
    return re.findall(r"^## (\S+)$", text, flags=re.M)


def _missing_domain_sections(subject: Subject) -> list[str]:
    found = set(_sections(subject.text))
    return [
        f"no `## {domain}` section"
        for domain in sorted(set(_POINTED_AGENTS.values()) - found)
    ]


def _sections_naming_no_domain(subject: Subject) -> list[str]:
    return [
        f"section `## {name}` names no findings.py domain"
        for name in _sections(subject.text)
        if name not in DOMAINS
    ]


def _agent_pointer_offence(subject: Subject) -> list[str]:
    domain = _POINTED_AGENTS[Path(subject.rel).name]
    designs = Path(_DESIGNS).name
    problems = []
    if f"`## {domain}`" not in subject.text or designs not in subject.text:
        problems.append(f"does not point at the `## {domain}` section of {designs}")
    if f"`### {domain}`" not in subject.text or _REGISTER not in subject.text:
        problems.append(f"does not point at the `### {domain}` table of {_REGISTER}")
    return problems


_AGENT_REASON = (
    "The design facts live once, in `.claude/skills/homelab-review/accepted-designs.md`, one "
    "section per reviewer domain, and the findings register (`--accepted` / `--refuted` "
    "closes) is rendered by the docs-refresh cron into the *Settled findings* table of "
    "`docs/reference/backlog.md`. An inline list in each agent would drift. This guards the "
    "shape, not the content: a pointer dropping out, or a section heading naming a domain "
    "`findings.py` does not know. An inline list creeping back into an agent is the "
    "`agents-carry-no-inline-reflag-list` row of `test_census_rows_text.py`. What a reviewer "
    "does with the list is a session's judgment, which no test can see."
)

ACCEPTED_DESIGNS_HOLD_EACH_POINTED_DOMAIN = Census(
    name="accepted-designs-hold-a-section-per-pointed-domain",
    reason=_AGENT_REASON,
    files=lambda: tracked(_DESIGNS),
    offence=_missing_domain_sections,
    red=(Subject(_DESIGNS, "## container\n## cicd\n"), Subject(_DESIGNS, "intro\n")),
    green=(Subject(_DESIGNS, _DOMAIN_SECTIONS),),
    must_find=frozenset({_DESIGNS}),
)

ACCEPTED_DESIGNS_SECTIONS_ARE_DOMAINS = Census(
    name="accepted-designs-sections-are-findings-domains",
    reason=(
        "A section heading is a `domain/<name>` label, so the register and the file group "
        "alike. " + _AGENT_REASON
    ),
    files=lambda: tracked(_DESIGNS),
    offence=_sections_naming_no_domain,
    red=(Subject(_DESIGNS, "## container\n## not-a-domain\n"),),
    green=(Subject(_DESIGNS, _DOMAIN_SECTIONS),),
    must_find=frozenset({_DESIGNS}),
)

_AGENT = ".claude/agents/homelab-cicd-reviewer.md"
_AGENT_POINTERS = f"`## cicd` accepted-designs.md `### cicd` {_REGISTER}\n"

REVIEWER_AGENTS_POINT_AT_THEIR_DOMAIN = Census(
    name="pointed-reviewer-agents-name-their-domain-in-both-sources",
    reason=_AGENT_REASON,
    files=lambda: tracked(*(f".claude/agents/{name}" for name in _POINTED_AGENTS)),
    offence=_agent_pointer_offence,
    red=(
        Subject(_AGENT, "`### cicd` " + _REGISTER),
        Subject(_AGENT, "`## cicd` accepted-designs.md `### cicd`\n"),
        Subject(_AGENT, "nothing\n"),
    ),
    green=(Subject(_AGENT, _AGENT_POINTERS),),
    must_find=frozenset(f".claude/agents/{name}" for name in _POINTED_AGENTS),
)


# ── The karakeep time-tagger fork ─────────────────────────────────────────────────────

_TAGGER = "k8s/roles/karakeep/files/karakeep-time-tagger.py"
_KARAKEEP_DEFAULTS = "k8s/roles/karakeep/defaults/main.yml"
_FORK_SHA256 = "f6baff066cb6af85b655593389bf7ba84e3cfd8a9c22d62fd2739a8d6ee240ec"
_UPSTREAM_URL = re.compile(
    r"https://raw\.githubusercontent\.com/\S+/karakeep-time-tagger\.py"
)
_IMMUTABLE_URL = re.compile(r"/raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/")


def _fork_hash_offence(subject: Subject) -> list[str]:
    actual = hashlib.sha256(subject.text.encode()).hexdigest()
    if actual == _FORK_SHA256:
        return []
    return [f"hashes {actual}, the row records {_FORK_SHA256}"]


def _upstream_pin_offence(subject: Subject) -> list[str]:
    urls = _UPSTREAM_URL.findall(subject.text)
    if not urls:
        return ["names no upstream URL for the time-tagger fork"]
    return [
        f"{url} does not pin a full commit SHA"
        for url in urls
        if not _IMMUTABLE_URL.search(url)
    ]


_PIN_COMMENT = (
    "# Forked from https://raw.githubusercontent.com/thiswillbeyourgithub/karakeep_python_api/"
    "37fa33fdd62b2ee5275ce8cdc1c86d29d0ec4237/community_scripts/karakeep-time-tagger/"
    "karakeep-time-tagger.py\n"
)

KARAKEEP_TIME_TAGGER_MATCHES_ITS_SHA256 = Census(
    name="karakeep-time-tagger-script-matches-its-pinned-sha256",
    reason=(
        "The karakeep role carries a fork of upstream's time-tagger script: upstream's file "
        "with one filtered INFO handler, no file sink and no non-TTY progress bar. A fork has "
        "no upstream bytes to match, so this holds the copy to its own sha256. The script is "
        "vendored because a ConfigMap renders from a file that must be present at render "
        "time, and CI renders every manifest template on a machine that has never run a "
        "deploy. To change the script, edit it and update the row's recorded sha256 "
        "together; to rebase onto a newer upstream, re-apply the changes to the new commit "
        "and update the upstream URL in the role's defaults as well."
    ),
    files=lambda: tracked(_TAGGER),
    offence=_fork_hash_offence,
    red=(Subject(_TAGGER, _real(_TAGGER) + "# an edit FORK_SHA256 does not record\n"),),
    green=(Subject(_TAGGER),),
    must_find=frozenset({_TAGGER}),
)

KARAKEEP_TIME_TAGGER_NAMES_AN_IMMUTABLE_COMMIT = Census(
    name="karakeep-time-tagger-fork-names-an-immutable-upstream-commit",
    reason=(
        "A branch or tag ref would make the checksum a snapshot of whatever upstream "
        "happened to be serving, so a legitimate re-download could silently change what runs."
    ),
    files=lambda: tracked(_KARAKEEP_DEFAULTS),
    offence=_upstream_pin_offence,
    red=(
        Subject(
            _KARAKEEP_DEFAULTS,
            _PIN_COMMENT.replace("37fa33fdd62b2ee5275ce8cdc1c86d29d0ec4237", "main"),
        ),
        Subject(_KARAKEEP_DEFAULTS, "# no pin here\n"),
    ),
    green=(Subject(_KARAKEEP_DEFAULTS, _PIN_COMMENT),),
    must_find=frozenset({_KARAKEEP_DEFAULTS}),
)


CONFIG_ROWS = (
    FIX_SKEPTIC_PASS_INSTRUCTED,
    ACCEPTED_DESIGNS_HOLD_EACH_POINTED_DOMAIN,
    ACCEPTED_DESIGNS_SECTIONS_ARE_DOMAINS,
    REVIEWER_AGENTS_POINT_AT_THEIR_DOMAIN,
    PREK_DEFAULTS_TO_THE_COMMIT_STAGE,
    PREK_REMOTE_HOOKS_PIN_THE_COMMIT_STAGE,
    PREK_UV_ENTRIES_ARE_FROZEN,
    GITLEAKS_MANIFEST_DIGEST_ALLOWLIST,
    GITLEAKS_SECRET_KEY_NAME_ALLOWLIST,
    KARAKEEP_TIME_TAGGER_MATCHES_ITS_SHA256,
    KARAKEEP_TIME_TAGGER_NAMES_AN_IMMUTABLE_COMMIT,
    PROMOTION_STALLED_WAITS_OUT_THE_REBOOT_WINDOW,
    EXPORTER_ABSENT_PAGES_A_MISSING_WINDOW_GAUGE,
    FLUX_NOT_READY_COMMENT_NAMES_THE_SCRAPE_PAGER,
)
