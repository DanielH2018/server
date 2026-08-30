"""Tree-wide guard: root that drops ALL capabilities must add a DAC capability back.

WHY THIS EXISTS. In this cluster's pods, `runAsUser: 0` with `drop: [ALL]` is WEAKER than
running as the pod's own uid, not stronger. Root's ability to ignore file permission bits is
the DAC_OVERRIDE capability, so dropping ALL takes it away — and root then cannot read or write
files owned by another uid, which is the only reason anyone reaches for root in the first place.

Both live sites record the lesson in a comment beside the fix, which is why this guard has real
accept cases rather than synthetic ones:

  * `loki-homelab/templates/promtail-daemonset.yaml.j2` adds DAC_READ_SEARCH — syslog/auth.log
    are owned by the `syslog` user at 640, so root without it cannot read them.
  * `code-server/templates/deployment.yaml.j2` adds CHOWN + DAC_OVERRIDE + FOWNER — a fresh
    claim's root is root:root while the files being copied belong to the pod uid.

`seed-volume/templates/seed-pod.yaml.j2` is the third `runAsUser: 0` site and drops nothing, so
root keeps its default capability set. That is clean and must stay clean: the hazard is the
COMBINATION, never root by itself.

WHAT THE REAL-TREE ASSERTION IS WORTH HERE. Zero violations today, so the real-tree half passing
is not by itself evidence the rule works — a rule matching nothing would pass identically. The
synthetic reject cases below are that evidence. What the real tree does buy is genuine accept
coverage: three production securityContexts exercise the clean paths.
"""

from __future__ import annotations

import re
from pathlib import Path

from _helpers import REPO as _REPO_ROOT
from _helpers import ROLES as _ROLES

# The two capabilities that give root back its permission-bit override. DAC_OVERRIDE is the
# read+write form; DAC_READ_SEARCH is the read-only form, which is enough for a log tailer.
_DAC_CAPS = ("DAC_OVERRIDE", "DAC_READ_SEARCH")

_ROOT = re.compile(r"^\s*runAsUser:\s*0\s*$")
_DROP_ALL = re.compile(r"^\s*-\s*ALL\s*$")
_ADD = re.compile(r"^\s*add:\s*$")
_DROP = re.compile(r"^\s*drop:\s*$")


def _manifest_files() -> list[Path]:
    return sorted(p for p in _ROLES.rglob("templates/*.j2") if "archive" not in p.parts)


def root_without_dac(text: str) -> list[int]:
    """Line numbers of `runAsUser: 0` blocks that drop ALL and add no DAC capability.

    Scans the securityContext following each `runAsUser: 0` rather than parsing YAML, because
    these are Jinja templates: a `{% if %}` around a block is legal here and would make a YAML
    parse fail on a file this guard still needs to read.
    """
    lines = text.splitlines()
    hits = []
    for i, line in enumerate(lines):
        if not _ROOT.match(line):
            continue
        indent = len(line) - len(line.lstrip())
        drops_all = adds_dac = False
        section = None
        for later in lines[i + 1 :]:
            if later.strip() and (len(later) - len(later.lstrip())) < indent:
                break  # left this securityContext
            if _DROP.match(later):
                section = "drop"
            elif _ADD.match(later):
                section = "add"
            elif section == "drop" and _DROP_ALL.match(later):
                drops_all = True
            elif section == "add" and any(cap in later for cap in _DAC_CAPS):
                adds_dac = True
        if drops_all and not adds_dac:
            hits.append(i + 1)
    return hits


def test_no_manifest_runs_root_with_all_capabilities_dropped() -> None:
    """The real tree. See the module docstring on what this passing does and does not prove."""
    offenders = []
    for path in _manifest_files():
        for line_no in root_without_dac(path.read_text()):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{line_no}")

    assert not offenders, (
        "these containers run as root with `drop: [ALL]` and add no DAC capability, which "
        "makes root WEAKER than the pod's own uid — it cannot read or write another uid's "
        "files. Add DAC_OVERRIDE (read+write) or DAC_READ_SEARCH (read-only), or do not run "
        "as root:\n  " + "\n  ".join(offenders)
    )


def test_root_dropping_all_with_no_dac_is_flagged() -> None:
    doc = """
        securityContext:
          runAsUser: 0
          capabilities:
            drop:
              - ALL
    """
    assert root_without_dac(doc) == [3]


def test_root_dropping_all_that_adds_dac_override_is_clean() -> None:
    """code-server's live shape: it needs to read the pod uid's files and hand them back."""
    doc = """
        securityContext:
          runAsUser: 0
          capabilities:
            drop:
              - ALL
            add:
              - CHOWN
              - DAC_OVERRIDE
              - FOWNER
    """
    assert root_without_dac(doc) == []


def test_root_dropping_all_that_adds_only_read_search_is_clean() -> None:
    """promtail's live shape: read-only is enough for a log tailer, so the narrower cap counts."""
    doc = """
        securityContext:
          runAsUser: 0
          capabilities:
            drop:
              - ALL
            add:
              - DAC_READ_SEARCH
    """
    assert root_without_dac(doc) == []


def test_root_that_drops_nothing_is_clean() -> None:
    """seed-pod's live shape. The hazard is the COMBINATION; root alone keeps DAC_OVERRIDE."""
    doc = """
        securityContext:
          runAsUser: 0
    """
    assert root_without_dac(doc) == []


def test_a_non_root_container_dropping_all_is_clean() -> None:
    """Dropping ALL is the correct default for an ordinary uid and must not be flagged."""
    doc = """
        securityContext:
          runAsUser: 1000
          capabilities:
            drop:
              - ALL
    """
    assert root_without_dac(doc) == []


def test_a_dac_cap_added_to_a_LATER_container_does_not_excuse_this_one() -> None:
    """The scan must stop at the end of this securityContext, or one fix would clear them all."""
    doc = """
        containers:
          - name: bad
            securityContext:
              runAsUser: 0
              capabilities:
                drop:
                  - ALL
          - name: good
            securityContext:
              runAsUser: 0
              capabilities:
                drop:
                  - ALL
                add:
                  - DAC_OVERRIDE
    """
    assert root_without_dac(doc) == [5]
