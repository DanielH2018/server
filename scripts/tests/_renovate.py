"""renovate.json as the Renovate guards read it, plus the packageRules walk they share.

Which dependency Renovate extracts from which file is not worked out here: CI runs Renovate's
own extract and holds it against `scripts/validate/renovate_expected_deps.tsv`
(`scripts/validate/renovate_extract.py`). A guard that needs the extracted deps reads that
table as `EXPECTED_DEPS`. What this module emulates is the packageRules half, which the extract
does not cover: the grouping, automerge and soak settings a dep resolves to.
"""

import json
import re
from typing import NamedTuple

from _ratchet_census import tracked_files
from lib import yaml_fast
from lib.repo_paths import REPO
from validate.renovate_extract import TABLE, Row, read_table

# Re-export for modules that import _REPO from this module
_REPO = REPO

_RENOVATE_CONFIG = json.loads((REPO / "renovate.json").read_text())

# The custom managers as written in renovate.json.
_CONFIG_MANAGERS = _RENOVATE_CONFIG["customManagers"]

_PACKAGE_RULES = _RENOVATE_CONFIG["packageRules"]


EXPECTED_DEPS = read_table(TABLE.read_text())


def expected_deps(file: str, dep_name: str) -> list[Row]:
    """The table's rows for one dep in one file, one per occurrence Renovate extracts."""
    return [d for d in EXPECTED_DEPS if d.file == file and d.dep_name == dep_name]


def workflow_env_pins(
    workflow: str, version_key: str, digest_key: str
) -> list[tuple[str, str]]:
    """Each (version, digest) a workflow's `env:` blocks set, at workflow, job and step level."""
    doc = yaml_fast.safe_load((REPO / workflow).read_text(encoding="utf-8"))
    blocks = [doc.get("env") or {}]
    for job in doc["jobs"].values():
        blocks.append(job.get("env") or {})
        blocks.extend(step.get("env") or {} for step in job.get("steps", []))
    return [(b[version_key], b[digest_key]) for b in blocks if version_key in b]


def _tracked_files() -> list[str]:
    return tracked_files()


def _slash_regex(pattern: str) -> re.Pattern:
    """A Renovate `matchCurrentValue` value, wrapped /like/this/."""
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
    test_no_k8s_role_image_is_disabled_by_a_package_rule).

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
    where at least one of its globs matches. A rule without file scoping would match every
    `currentValue: latest` regardless of file, silently disabling deliberately digest-pinned
    images.
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


# Both n8n FROMs pin a channel tag with a digest beside it, so a digest bump shows no version
# string. The ledger beside the Dockerfiles records the version each digest carries; the helpers
# here parse it, and test_renovate_dockerfiles.py asserts against it.
N8N_ROLE_DIR = REPO / "k8s/roles/n8n"
N8N_PIN_HISTORY = N8N_ROLE_DIR / "base-pin-history.tsv"

# Named rather than globbed: a census that discovers its subjects returns an empty set after a
# rename, and `all()` over nothing passes.
N8N_PINS = {
    "n8nio/n8n": "templates/Dockerfile.j2",
    "n8nio/runners": "templates/Dockerfile-runners.j2",
}

# A channel pin follows what upstream promotes, so moving back is legitimate; moving back silently
# is not.
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


def _resolve_setting(
    field: str,
    dep_name: str,
    rel_path: str,
    update_type: str,
    datasource: str,
    rules: list[dict] = _PACKAGE_RULES,
    dep_type: str | None = None,
    manager: str = "custom.regex",
):
    """The value of `field` Renovate would resolve for a dep, walking packageRules in order.

    Later rules win a field they set, so the last matching rule that sets `field` decides —
    this mirrors `_is_disabled_by_packagerule`'s walk, for an arbitrary field instead of
    `enabled`. Rule ORDER is what this reads, which is why a guard on a rule that overrides an
    earlier one asserts through here rather than against the rule's own body. `dep_type` is
    the manager's `depTypeTemplate`; a rule with `matchDepTypes` matches only a dep carrying
    one of them. `manager` is the manager that extracts the dep; it defaults to custom.regex,
    which extracts every dep the other guards ask about.
    """
    value = None
    for rule in rules:
        if "matchManagers" in rule and manager not in rule["matchManagers"]:
            continue
        if "matchDepTypes" in rule and dep_type not in rule["matchDepTypes"]:
            continue
        if "matchFileNames" in rule and not any(
            _minimatch_to_regex(g).match(rel_path) for g in rule["matchFileNames"]
        ):
            continue
        if "matchDatasources" in rule and datasource not in rule["matchDatasources"]:
            continue
        if "matchPackageNames" in rule and not any(
            re.search(n[1:-1], dep_name)
            if n.startswith("/") and n.endswith("/")
            else n == dep_name
            for n in rule["matchPackageNames"]
        ):
            continue
        if "matchUpdateTypes" in rule and update_type not in rule["matchUpdateTypes"]:
            continue
        if field in rule:
            value = rule[field]
    return value


def _resolve_group_name(
    dep_name: str,
    rel_path: str,
    update_type: str,
    datasource: str,
    rules: list[dict] = _PACKAGE_RULES,
    dep_type: str | None = None,
) -> str | None:
    """The `groupName` Renovate would resolve for a dep. `_resolve_setting` for one field."""
    return _resolve_setting(
        "groupName", dep_name, rel_path, update_type, datasource, rules, dep_type
    )
