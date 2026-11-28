#!/usr/bin/env python3
"""Check Renovate's own extract of this repo against the expected-dependency table.

`renovate_config.sh extract` runs `renovate --platform=local --dry-run=extract` and hands the
report it writes to `check`. Renovate itself does the matching, so no Python copy of its regex
managers can drift from what the hosted app reads. The check fails on six things:

- a row of `renovate_expected_deps.tsv` that the extract no longer finds, which is what a
  broken matchString or a reformatted pin produces, or a dep the table does not list;
- a regex dep whose `autoReplaceStringTemplate`, rendered with the values it captured, does not
  reproduce the text it matched, so the next bump would rewrite the line into another shape;
- a regex dep whose captured value stops short of the version beside it (`1.2` out of
  `1.2.3`), so the next bump writes `1.3.3`;
- a pin line that no extracted dep's span covers: a `# renovate:` comment, an `_image:` key in
  the k8s defaults, a control-plane `_version:` key, a version a pinned download renders from,
  or a second occurrence of a release-download version that a bump would leave behind;
- a tracked file holding a release-download URL that no manager extracts from;
- an annotated pin that `lib.renovate_annotations`, the reader `asset_pins.py` refreshes
  digests with, reads differently from Renovate.

The table holds identity and shape (manager, file, depName, datasource, whether a digest is
captured, and packageName where it differs from depName), never the version itself, so a Renovate bump PR leaves it unchanged. github-actions
and pep621 are left out: their deps are every `uses:` line and every pyproject dependency,
which Renovate's built-in parsers find without any config of ours.

Typical usage example:

    renovate_extract.py check "$RUNNER_TEMP/renovate-extract.json"
    renovate_extract.py write report.json     # regenerate the table after adding a pin
    renovate_extract.py scope changed.txt --added added.txt   # true when the PR can move it
"""

import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path as _Path
from typing import NamedTuple

from lib.cli_help import ArgumentParser
from lib.git import git_stdout
from lib.renovate_annotations import annotations_in, is_annotation_manager
from lib.repo_paths import REPO

TABLE = REPO / "scripts/validate/renovate_expected_deps.tsv"
# The annotation reader's path comes from the module, because a literal path here reads to
# lib/script_classify.py as this script invoking it.
CHECK_FILES = frozenset(
    {
        "scripts/validate/renovate_expected_deps.tsv",
        "scripts/validate/renovate_extract.py",
        "scripts/validate/renovate_config.sh",
        "scripts/validate/asset_pins.py",
        str(
            _Path(sys.modules[annotations_in.__module__].__file__ or "")
            .resolve()
            .relative_to(REPO)
        ),
    }
)
UNTABLED_MANAGERS = frozenset({"github-actions", "pep621"})
TABLE_HEADER = (
    "# Every dependency Renovate's extract finds, one row per occurrence, sorted.\n"
    "# scripts/validate/renovate_extract.py checks CI's extract against it; after adding or\n"
    "# moving a pin, regenerate it with `renovate_extract.py write <report>`.\n"
    "# manager\tfile\tdepName\tdatasource\tdigest\tpackageName\n"
)

# Images this repo builds and pushes to the node-local registry. Renovate watches each one's
# Dockerfile FROM instead, so the `_image:` key itself has nothing to track.
REGISTRY_BUILT_IMAGES = frozenset(
    {
        "airflow_k8s_image",
        "airflow_k8s_task_image",
        "n8n_k8s_image",
        "n8n_k8s_runners_image",
        "ical_proxy_k8s_image",
        "code_server_k8s_image",
        "homelab_mcp_k8s_image",
        "karakeep_k8s_tagger_image",
        "nut_k8s_image",
        "pi_peer_backup_k8s_image",
        "terraria_k8s_image",
        "valheim_k8s_mods_image",
        "vault_mcp_k8s_image",
    }
)

IMAGE_DEFAULTS = (
    "k8s/roles/*/defaults/main.yml",
    "hosts/components/registry-gc/defaults/main.yml",
    "k8s/vars.yml",
)
CONTROL_PLANE_DEFAULTS = "k8s/roles/system-upgrade/defaults/main.yml"

# Each jellyfin plugin's release tag is the leading part of its four-part version, so the
# matchStrings capture the tag and rewrite every occurrence in step.
PARTIAL_VALUE_DEPS = frozenset(
    {
        ("k8s/roles/jellyfin/defaults/main.yml", "vosmiic/jellyfin-ani-sync"),
        ("k8s/roles/jellyfin/defaults/main.yml", "jellyfin/jellyfin-plugin-webhook"),
        (
            "k8s/roles/jellyfin/defaults/main.yml",
            "shemanaev/jellyfin-plugin-media-cleaner",
        ),
    }
)

# Version keys a pinned download renders from that no manager tracks, on purpose.
UNTRACKED_BY_DECISION = {
    # Covers traefik's in-tree CRD copy, which Renovate cannot rewrite: the image pin is the
    # tracked one, and tests/k8s/edge/test_traefik_crds.py fails a minor image bump until the copy,
    # this key and the hash are refreshed together.
    "traefik_crds_k8s_version": "follows traefik_k8s_image's minor; refreshed by hand",
}

# Each census pattern's `pin` group is the text an extracted dep's span must contain.
_PIN_VALUE = r"[ \t]*[\"']?(?P<pin>[^\s\"'#]+)"
_IMAGE_LINE = re.compile(rf"(?m)^[ \t]*(?P<key>\w*_image):{_PIN_VALUE}")
_CONTROL_PLANE_LINE = re.compile(
    rf"(?m)^(?P<key>[a-z0-9_]*_version):(?=[ \t]*\"?v){_PIN_VALUE}"
)
_ANNOTATION_LINE = re.compile(r"(?m)^[ \t]*(?P<pin>#[ \t]*renovate:.*?)[ \t]*$")
_RELEASE_URL = re.compile(r"releases/download/v([\d.]+)/")
# `#` to end of line, at line start or after a space, so a `#` fragment in a URL is not one.
_COMMENT = re.compile(r"(?m)(?:(?<=^)|(?<=\s))#[^\n]*")


class Row(NamedTuple):
    """One row of the expected-dependency table.

    Attributes:
        manager: the Renovate manager, `regex` for every custom manager.
        file: the repo-relative package file.
        dep_name: Renovate's depName.
        datasource: Renovate's datasource.
        digest: `digest` when the dep captures one, else `-`.
        package_name: Renovate's packageName where it differs from depName, else `-`.
    """

    manager: str
    file: str
    dep_name: str
    datasource: str
    digest: str
    package_name: str

    def identity(self) -> tuple[str, ...]:
        """Everything but the digest column, which `table_drift` compares one way."""
        return (
            self.manager,
            self.file,
            self.dep_name,
            self.datasource,
            self.package_name,
        )

    def names(self) -> set[str]:
        """The names a packageRule's `matchPackageNames` can match this dep by."""
        return {self.dep_name} | ({self.package_name} - {"-"})


@dataclass(frozen=True)
class Dep:
    """One dependency as the extract reports it.

    Attributes:
        manager: the Renovate manager, `regex` for every custom manager.
        file: the repo-relative package file.
        dep_name: Renovate's depName.
        datasource: Renovate's datasource.
        current_value: the captured version, empty when the dep carries none.
        current_digest: the captured digest, empty when the dep carries none.
        replace_string: the exact text the matchString matched, empty for a built-in manager.
        template: the custom manager's autoReplaceStringTemplate, empty when it sets none.
        package_name: Renovate's packageName, empty when it equals depName.
        dep_type: Renovate's depType, empty when the manager sets none.
    """

    manager: str
    file: str
    dep_name: str
    datasource: str
    current_value: str = ""
    current_digest: str = ""
    replace_string: str = ""
    template: str = ""
    package_name: str = ""
    dep_type: str = ""

    def row(self) -> Row:
        return Row(
            self.manager,
            self.file,
            self.dep_name,
            self.datasource,
            "digest" if self.current_digest else "-",
            self.package_name or "-",
        )


def deps_in(report: dict) -> list[Dep]:
    """Every dep in a Renovate file report that pins a value or a digest."""
    (repo,) = report["repositories"].values()
    deps = []
    for manager, package_files in repo["packageFiles"].items():
        for package_file in package_files:
            for dep in package_file.get("deps") or []:
                if not (dep.get("currentValue") or dep.get("currentDigest")):
                    continue
                deps.append(
                    Dep(
                        manager=manager,
                        file=package_file["packageFile"],
                        dep_name=dep.get("depName") or "",
                        datasource=dep.get("datasource") or "",
                        current_value=dep.get("currentValue") or "",
                        current_digest=dep.get("currentDigest") or "",
                        replace_string=dep.get("replaceString") or "",
                        template=package_file.get("autoReplaceStringTemplate") or "",
                        package_name=_package_name(dep),
                        dep_type=dep.get("depType") or "",
                    )
                )
    return deps


def _package_name(dep: dict) -> str:
    name = dep.get("packageName") or ""
    return "" if name == dep.get("depName") else name


def table_rows(deps: Iterable[Dep]) -> list[Row]:
    return sorted(d.row() for d in deps if d.manager not in UNTABLED_MANAGERS)


def format_table(rows: Iterable[Row]) -> str:
    return TABLE_HEADER + "".join("\t".join(r) + "\n" for r in rows)


def read_table(text: str) -> list[Row]:
    return [
        Row(*line.split("\t"))
        for line in text.splitlines()
        if line and not line.startswith("#")
    ]


def table_drift(expected: Iterable[Row], actual: Iterable[Row]) -> list[str]:
    """One line per dep the table and the extract disagree on, the identity tab-separated.

    Deps compare on `Row.identity`, as a multiset. The digest column fails only one way: a dep
    the table records with a digest that comes back without one, which is what a matchString
    that stopped capturing it produces. A dep gaining a digest is Renovate's own pinDigest
    update landing, so it passes.
    """
    expected, actual = list(expected), list(actual)
    want = Counter(r.identity() for r in expected)
    got = Counter(r.identity() for r in actual)
    want_digest = Counter(r.identity() for r in expected if r.digest == "digest")
    got_digest = Counter(r.identity() for r in actual if r.digest == "digest")
    lost = (want_digest - got_digest) & got
    return (
        [f"not extracted:\t{chr(9).join(r)}" for r in sorted((want - got).elements())]
        + [
            f"not in the table:\t{chr(9).join(r)}"
            for r in sorted((got - want).elements())
        ]
        + [
            f"digest no longer captured:\t{chr(9).join(r)}"
            for r in sorted(lost.elements())
        ]
    )


_IF_BLOCK = re.compile(r"\{\{#if (\w+)\}\}(.*?)\{\{/if\}\}", re.S)
_TRIPLE_STACHE = re.compile(r"\{\{\{(\w+)\}\}\}")


def render_auto_replace(template: str, **values: str | None) -> str:
    """Render an `autoReplaceStringTemplate` the way Renovate's Handlebars would.

    Covers `{{{name}}}` and a plain `{{#if name}}…{{/if}}`, the two constructs this repo's
    templates use. An unset value renders empty, as in Handlebars.

    Raises:
        AssertionError: the template reads a name not in `values`, or uses a third construct.
    """

    def _block(m: re.Match[str]) -> str:
        assert m.group(1) in values, f"template gates on unknown var {m.group(1)!r}"
        return m.group(2) if values[m.group(1)] else ""

    def _var(m: re.Match[str]) -> str:
        assert m.group(1) in values, f"template reads unknown var {m.group(1)!r}"
        return values[m.group(1)] or ""

    out = _TRIPLE_STACHE.sub(_var, _IF_BLOCK.sub(_block, template))
    assert "{{" not in out, f"unrendered Handlebars left in {out!r}"
    return out


def broken_templates(deps: Iterable[Dep]) -> list[str]:
    """Each regex dep whose template, given its own captured values, rewrites its span."""
    problems = []
    for d in deps:
        if d.manager != "regex" or not d.template:
            continue
        rendered = render_auto_replace(
            d.template,
            depName=d.dep_name,
            newValue=d.current_value,
            newDigest=d.current_digest or None,
        )
        if rendered != d.replace_string:
            problems.append(f"{d.file}: {d.replace_string!r} -> {rendered!r}")
    return problems


def truncated_values(deps: Iterable[Dep]) -> list[str]:
    """Each regex dep whose captured value is followed in its own span by more version digits.

    A matchString capturing `1.2` out of `X_VERSION: 1.2.3` still covers the pin line, so only
    this check sees it, and the next bump writes `1.3.3`. PARTIAL_VALUE_DEPS are deliberate.
    """
    problems = []
    for d in deps:
        if d.manager != "regex" or not d.current_value or not d.replace_string:
            continue
        if (d.file, d.dep_name) in PARTIAL_VALUE_DEPS:
            continue
        ends = [
            m.end() for m in re.finditer(re.escape(d.current_value), d.replace_string)
        ]
        if ends and all(re.match(r"\.?\d", d.replace_string[e:]) for e in ends):
            problems.append(
                f"{d.file}: {d.dep_name} captured {d.current_value!r} out of "
                f"{d.replace_string!r}"
            )
    return problems


def _spans(text: str, needles: Iterable[str]) -> list[tuple[int, int]]:
    spans = []
    for needle in set(needles):
        spans.extend(m.span() for m in re.finditer(re.escape(needle), text))
    return spans


def _covered(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


@dataclass(frozen=True)
class PinLine:
    """The pinned text on one line of a tracked file, which some extracted dep's span must contain."""

    file: str
    start: int
    end: int
    why: str


def pin_lines(
    files: Iterable[str],
    read: Callable[[str], str],
    pinned_versions: Iterable[tuple[str, str]] = (),
) -> list[PinLine]:
    """Every pin line the census rules name in `files`.

    `pinned_versions` is (file, key) for each version key a pinned download renders from.
    """
    lines = []

    def add(path: str, text: str, pattern: re.Pattern, why: str, skip=frozenset()):
        for m in pattern.finditer(text):
            if "key" in pattern.groupindex and m.group("key") in skip:
                continue
            lines.append(PinLine(path, m.start("pin"), m.end("pin"), why))

    for path in files:
        if any(fnmatch(path, g) for g in IMAGE_DEFAULTS):
            add(
                path, read(path), _IMAGE_LINE, "an `_image:` key", REGISTRY_BUILT_IMAGES
            )
        if path == CONTROL_PLANE_DEFAULTS:
            add(path, read(path), _CONTROL_PLANE_LINE, "a control-plane version")
        if not path.endswith((".md", ".py")) and path != "renovate.json":
            text = read(path)
            if "renovate:" in text:
                add(path, text, _ANNOTATION_LINE, "a `# renovate:` annotation")
    for path, key in pinned_versions:
        if key in UNTRACKED_BY_DECISION:
            continue
        pattern = re.compile(rf"(?m)^[ \t]*{re.escape(key)}:{_PIN_VALUE}")
        add(path, read(path), pattern, "the version a pinned download renders from")
    return lines


def uncovered_pin_lines(
    lines: Iterable[PinLine], deps: list[Dep], read: Callable[[str], str]
) -> list[str]:
    spans_by_file: dict[str, list[tuple[int, int]]] = {}
    problems = []
    for line in lines:
        if line.file not in spans_by_file:
            spans_by_file[line.file] = _spans(
                read(line.file),
                (
                    d.replace_string
                    for d in deps
                    if d.file == line.file and d.replace_string
                ),
            )
        if not _covered(line.start, line.end, spans_by_file[line.file]):
            text = read(line.file)
            excerpt = text[line.start : line.end].strip()
            problems.append(
                f"{line.file}:{_line_no(text, line.start)}: {line.why} no manager reads: "
                f"{excerpt}"
            )
    return problems


def annotation_reader_drift(
    deps: Iterable[Dep], files: Iterable[str], read: Callable[[str], str]
) -> list[str]:
    """Each annotated pin the Python reader and Renovate's extract disagree on.

    `asset_pins.py` reads `# renovate:` annotations through `lib.renovate_annotations`,
    which runs renovate.json's annotation matchString as a Python regex. An RE2 construct
    Python reads differently would make it see other pins than Renovate does; this holds the
    two to the same (file, depName, datasource) multiset.
    """
    config = json.loads((REPO / "renovate.json").read_text())
    (manager,) = [m for m in config["customManagers"] if is_annotation_manager(m)]
    renovate = Counter(
        (d.file, d.dep_name, d.datasource)
        for d in deps
        if d.manager == "regex" and d.dep_type == manager["depTypeTemplate"]
    )
    python = Counter(
        (path, groups["depName"], groups["datasource"])
        for path in files
        if not is_release_url_exempt(path) and "renovate:" in (text := read(path))
        for groups in annotations_in(manager, text)
    )
    return [
        f"{f}: the Python annotation reader reads {name} ({ds}); Renovate does not"
        for f, name, ds in sorted((python - renovate).elements())
    ] + [
        f"{f}: Renovate reads the annotated pin {name} ({ds}); the Python reader does not"
        for f, name, ds in sorted((renovate - python).elements())
    ]


def is_release_url_exempt(path: str) -> bool:
    """Prose and code, where a release URL is a citation or a fixture, never a pin."""
    return path.endswith((".md", ".py")) or path == "renovate.json"


def _release_url_pins(
    files: Iterable[str], read: Callable[[str], str]
) -> Iterable[tuple[str, str, str, set[str]]]:
    """(path, text, text with comments blanked, versions) for each file with a release URL."""
    for path in files:
        if is_release_url_exempt(path):
            continue
        text = read(path)
        code = _COMMENT.sub(lambda m: " " * len(m.group()), text)
        versions = {m.group(1) for m in _RELEASE_URL.finditer(code)}
        if versions:
            yield path, text, code, versions


def release_url_files(files: Iterable[str], read: Callable[[str], str]) -> list[str]:
    """The files `release_url_problems` holds to the extract."""
    return [path for path, *_ in _release_url_pins(files, read)]


def release_url_problems(
    files: Iterable[str], deps: list[Dep], read: Callable[[str], str]
) -> list[str]:
    """Release-download URLs that a bump would miss, wholly or in part.

    A file holding one that no manager extracts from ages with no PR. In a file a manager does
    read, every occurrence of the URL's version must sit in some dep's span: the asset name
    repeats the tag (`download/v3.18.0/vale_3.18.0_Linux_64-bit.tar.gz`), and a manager that
    rewrites only the tag leaves a 404. Comments are blanked first, since no manager rewrites
    prose. The version matches as a free substring on purpose, so `4.1` inside the
    `4.1.0.0` of jellyfin's plugin must be covered too.
    """
    package_files = {d.file for d in deps}
    problems = []
    for path, text, code, versions in _release_url_pins(files, read):
        if path not in package_files:
            problems.append(f"{path}: a release-download URL no manager extracts from")
            continue
        spans = _spans(text, (d.replace_string for d in deps if d.file == path))
        for version in sorted(versions):
            for occ in re.finditer(re.escape(version), code):
                if not _covered(occ.start(), occ.end(), spans):
                    problems.append(
                        f"{path}:{_line_no(text, occ.start())}: release version {version} "
                        "outside every manager's span"
                    )
    return problems


def tracked_files() -> list[str]:
    out = git_stdout("ls-files", "-z", cwd=REPO)
    return [p for p in out.split("\0") if p and (REPO / p).is_file()]


def read_file(rel: str) -> str:
    return (REPO / rel).read_text(errors="replace")


def pinned_versions() -> list[tuple[str, str]]:
    from validate.asset_pins import discover_pins

    pins, _ = discover_pins()
    return [
        (pin.source, key)
        for pin in pins
        for key in sorted(pin.inputs)
        if key.endswith("_version")
    ]


def check(report: dict, table_text: str) -> list[str]:
    deps = deps_in(report)
    files = tracked_files()
    lines = pin_lines(files, read_file, pinned_versions())
    return (
        table_drift(read_table(table_text), table_rows(deps))
        + broken_templates(deps)
        + truncated_values(deps)
        + uncovered_pin_lines(lines, deps, read_file)
        + release_url_problems(files, deps, read_file)
        + annotation_reader_drift(deps, files, read_file)
    )


def needs_extract(
    changed: Iterable[str], table_text: str, added: Iterable[str] = ()
) -> bool:
    """Whether a PR changing `changed` and adding `added` can change what the extract finds.

    True for renovate.json, the table, a file the table lists, a file a census rule reads, the
    check itself, or an added file that is not prose or Python: no rule here can tell which
    new file a manager's pattern reaches, and a dep the table missed would fail every later
    PR that runs the extract.
    """
    if any(not is_release_url_exempt(p) and not p.startswith("docs/") for p in added):
        return True
    watched = {row.file for row in read_table(table_text)}
    watched |= {"renovate.json", *CHECK_FILES}
    for path in changed:
        if path in watched or any(fnmatch(path, g) for g in IMAGE_DEFAULTS):
            return True
        p = REPO / path
        if not is_release_url_exempt(path) and p.is_file():
            text = p.read_text(errors="replace")
            if "renovate:" in text or _RELEASE_URL.search(text):
                return True
    return False


def _path_lists(files: Iterable[_Path]) -> list[str]:
    return [
        line for f in files if f.exists() for line in f.read_text().splitlines() if line
    ]


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (
        ("check", "check a Renovate file report against the table"),
        ("write", "rewrite the table from a Renovate file report"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("report", type=_Path)
    p = sub.add_parser(
        "scope", help="print true when the changed files can move the extract"
    )
    p.add_argument(
        "changed", type=_Path, nargs="+", help="files listing one path per line"
    )
    p.add_argument(
        "--added", type=_Path, action="append", default=[], help="a file of added paths"
    )
    args = parser.parse_args(argv)

    if args.cmd == "scope":
        changed, added = _path_lists(args.changed), _path_lists(args.added)
        print("true" if needs_extract(changed, TABLE.read_text(), added) else "false")
        return 0
    report = json.loads(args.report.read_text())
    if args.cmd == "write":
        TABLE.write_text(format_table(table_rows(deps_in(report))))
        print(f"wrote {TABLE.relative_to(REPO)}")
        return 0
    problems = check(report, TABLE.read_text())
    deps = deps_in(report)
    if not problems:
        print(f"renovate extract: {len(deps)} deps, 0 problems")
        return 0
    print(
        f"renovate extract: {len(deps)} deps, {len(problems)} problems", file=sys.stderr
    )
    for line in problems:
        print(f"  {line}", file=sys.stderr)
    print(
        f"Renovate's report is {args.report}. If a pin was added or moved on purpose, "
        "regenerate scripts/validate/renovate_expected_deps.tsv with "
        f"`uv run python scripts/validate/renovate_extract.py write {args.report}`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
