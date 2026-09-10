"""renovate.json as the Renovate guards read it, plus the regex translations they share.

Renovate's `managerFilePatterns` and `matchStrings` are RE2 and minimatch; the helpers here
turn each into a Python `re` so a guard can run it over `git ls-files` and assert it still
matches something. The `tracked` fixture in conftest.py is the file list they run over.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import NamedTuple


_REPO = Path(__file__).resolve().parents[2]

_RENOVATE_CONFIG = json.loads((_REPO / "renovate.json").read_text())

_MANAGERS = _RENOVATE_CONFIG["customManagers"]

_PACKAGE_RULES = _RENOVATE_CONFIG["packageRules"]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, text=True, capture_output=True, check=True
    ).stdout
    return out.splitlines()


def _to_python_regex(pattern: str) -> str:
    # Renovate/RE2 named groups are (?<name>...); Python's re wants (?P<name>...).
    return re.sub(r"\(\?<(\w+)>", r"(?P<\1>", pattern)


def _file_pattern_to_regex(fp: str) -> re.Pattern:
    # A Renovate managerFilePattern wrapped in /.../ is a regex matched against the repo-relative
    # path (all of them use this /regex/ form). Strip the one leading + trailing slash.
    assert fp.startswith("/") and fp.endswith("/"), (
        f"expected a /regex/ file pattern: {fp}"
    )
    return re.compile(fp[1:-1])


def _slash_regex(pattern: str) -> re.Pattern:
    """A Renovate `matchCurrentValue` value, wrapped /like/this/ same as a managerFilePattern."""
    assert pattern.startswith("/") and pattern.endswith("/"), (
        f"expected a /regex/ matchCurrentValue: {pattern}"
    )
    return re.compile(pattern[1:-1])


def _minimatch_to_regex(glob: str) -> re.Pattern:
    """Renovate `matchFileNames` entries are minimatch globs, not regexes — `**` crosses `/`,
    a single `*` doesn't. Anchored full-match, repo-relative, forward-slash paths only."""
    out = []
    i = 0
    while i < len(glob):
        if glob[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _disabling_currentvalue_rules(package_rules: list[dict]) -> list[dict]:
    """packageRules that disable a dependency by matching its `currentValue` (e.g.

    the /^latest$/ rule that started this whole guard — see
    test_every_k8s_role_image_is_renovate_tracked).

    Deliberately excludes package-NAME disables (e.g. influxdb's `matchPackageNames` rule) — those
    are a targeted, reviewed exemption for one dependency, not a value-shaped trap that can silently
    swallow a whole class of images the way the currentValue rule did.
    """
    return [
        r
        for r in package_rules
        if r.get("enabled") is False and "matchCurrentValue" in r
    ]


def _is_disabled_by_packagerule(
    current_value: str, rel_path: str, rules: list[dict]
) -> dict | None:
    """The rule that disables `current_value` for `rel_path`, or None if none do.

    A rule with no `matchFileNames` applies everywhere; one with `matchFileNames` applies only
    where at least one of its globs matches. This is the check the pre-2026-08-13 disable rule
    lacked scoping for: it matched every `currentValue: latest` regardless of file, silently
    disabling 13 k8s roles' worth of deliberately digest-pinned images.
    """
    for rule in rules:
        if not _slash_regex(rule["matchCurrentValue"]).search(current_value):
            continue
        file_globs = rule.get("matchFileNames")
        if file_globs is None or any(
            _minimatch_to_regex(g).match(rel_path) for g in file_globs
        ):
            return rule
    return None


def _manager_covering(path_fragment: str) -> dict:
    """Selected by the paths a manager watches, not by its datasource.

    There is more than one `docker`-datasource manager now — the compose templates and the k8s
    role defaults — so picking the first match on datasource alone would silently hand back the
    wrong one and make a coverage test assert against files it never looks at.
    """
    for m in _MANAGERS:
        if m.get("datasourceTemplate") != "docker":
            continue
        if any(path_fragment in p for p in m["managerFilePatterns"]):
            return m
    raise AssertionError(f"no docker customManager covering {path_fragment!r}")


def _k8s_image_manager() -> dict:
    return _manager_covering("roles/k8s")


# ── the n8n base-pin ledger ──────────────────────────────────────────────────────────────
#
# Both n8n FROMs pin a channel tag with a digest beside it, so a bump changes only the digest
# and no version string appears in the diff for a reader or a test to compare. Renovate PR
# #1440 proposed a lockstep DOWNGRADE that way and passed every check (issue #1493). The
# ledger beside the Dockerfiles records the version each adopted digest carries; the helpers
# here parse it, and the guards in test_renovate_dockerfiles.py assert against it.
N8N_IMAGES_DIR = _REPO / "ansible/roles/k8s/n8n-images"
N8N_PIN_HISTORY = N8N_IMAGES_DIR / "base-pin-history.tsv"

# image -> the Dockerfile whose FROM must equal that image's last ledger entry. Named rather
# than globbed: a census that discovers its own subjects returns an empty set the moment a file
# is renamed, and an `all()` over nothing passes.
N8N_PINS = {
    "n8nio/n8n": "templates/Dockerfile.j2",
    "n8nio/runners": "templates/Dockerfile-runners.j2",
}

# The note that makes a version decrease deliberate. A channel pin follows what upstream
# promotes, so moving back with a withdrawn release is legitimate — what must not happen is
# moving back silently.
DOWNGRADE_ACK = "DOWNGRADE-ACK:"


class PinEntry(NamedTuple):
    version: str
    image: str
    digest: str
    note: str


def parse_pin_history(text: str) -> list[PinEntry]:
    """The ledger's data rows, in file order. Comments and blank lines are dropped.

    A malformed row raises rather than being skipped: a row this cannot read is a row the
    guards cannot check, and skipping it would turn a typo into a silent pass.
    """
    entries = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) < 3:
            raise ValueError(
                f"base-pin-history.tsv:{lineno}: expected tab-separated "
                f"version/image/digest[/note], got {raw!r}"
            )
        version, image, digest = (f.strip() for f in fields[:3])
        note = fields[3].strip() if len(fields) > 3 else ""
        if not re.fullmatch(r"\d+(\.\d+)*", version):
            raise ValueError(
                f"base-pin-history.tsv:{lineno}: {version!r} is not a dotted numeric version"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(
                f"base-pin-history.tsv:{lineno}: {digest!r} is not a sha256 digest"
            )
        entries.append(PinEntry(version, image, digest, note))
    return entries


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def unacknowledged_downgrades(entries: list[PinEntry]) -> list[str]:
    """One message per row whose version sits below the row before it for the same image.

    A row carrying DOWNGRADE_ACK in its note is a decision, not a defect, and is left alone.
    """
    flagged = []
    previous: dict[str, PinEntry] = {}
    for entry in entries:
        prior = previous.get(entry.image)
        if (
            prior is not None
            and version_key(entry.version) < version_key(prior.version)
            and DOWNGRADE_ACK not in entry.note
        ):
            flagged.append(
                f"{entry.image}: {prior.version} -> {entry.version} is a downgrade with no "
                f"{DOWNGRADE_ACK} note. If upstream withdrew the newer release, say so in "
                "the note column; otherwise this pin is a regression."
            )
        previous[entry.image] = entry
    return flagged


def last_pin_per_image(entries: list[PinEntry]) -> dict[str, PinEntry]:
    return {entry.image: entry for entry in entries}
