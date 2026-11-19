"""`renovate_extract.py`: the verdicts over synthetic Renovate reports, and the committed table.

Nothing here runs Renovate. CI's `renovate-config` job and the daily canary run it through
`renovate_config.sh extract`; this covers what the checker concludes from a report.

Run: uv run pytest scripts/validate/tests/test_validate_renovate_extract.py
"""

import json

from lib.repo_paths import REPO
from validate.renovate_extract import (
    TABLE,
    Dep,
    PinLine,
    Row,
    annotation_reader_drift,
    broken_templates,
    deps_in,
    format_table,
    needs_extract,
    pin_lines,
    pinned_versions,
    read_file,
    read_table,
    release_url_files,
    release_url_problems,
    render_auto_replace,
    table_drift,
    table_rows,
    tracked_files,
    truncated_values,
    uncovered_pin_lines,
)

DIGEST = "sha256:" + "ab" * 32
K8S_TEMPLATE = next(
    m["autoReplaceStringTemplate"]
    for m in json.loads((REPO / "renovate.json").read_text())["customManagers"]
    if any("k8s/roles" in p for p in m["managerFilePatterns"])
    and "autoReplaceStringTemplate" in m
)

# Pins whose digest moves in the same match as the version, so a manager that captured only
# the version would bump it beside a stale digest and fail the job's checksum.
DIGEST_PAIRS = [
    (
        "regex",
        ".github/workflows/ci.yml",
        "goreleaser/nfpm",
        "github-release-attachments",
    ),
    (
        "regex",
        ".github/workflows/ci.yml",
        "kubernetes-sigs/kind",
        "github-release-attachments",
    ),
    ("regex", ".github/workflows/ci.yml", "kindest/node", "docker"),
    (
        "regex",
        ".github/workflows/promote.yml",
        "sigstore/cosign",
        "github-release-attachments",
    ),
    (
        "regex",
        ".github/workflows/promote.yml",
        "fluxcd/flux2",
        "github-release-attachments",
    ),
    (
        "regex",
        "hosts/packages/homelab-tools/package.yml",
        "getsops/sops",
        "github-release-attachments",
    ),
]


def _report(*package_files: tuple[str, str, list[dict], str]) -> dict:
    return {
        "repositories": {
            "local": {
                "packageFiles": {
                    manager: [
                        {"packageFile": f, "deps": deps, "autoReplaceStringTemplate": t}
                        for m, f, deps, t in package_files
                        if m == manager
                    ]
                    for manager in {pf[0] for pf in package_files}
                }
            }
        }
    }


def _row(digest: str = "-", file: str = "a.yml", dep: str = "a/b") -> Row:
    return Row("regex", file, dep, "docker", digest, "-")


def _dep(file: str, replace: str, value: str = "1.0", digest: str = "", **kw) -> Dep:
    return Dep(
        "regex", file, "a/b", "docker", value, digest, replace, kw.get("template", "")
    )


# ── the table ─────────────────────────────────────────────────────────────────────────────


def test_the_committed_table_holds_every_digest_pair_with_its_digest() -> None:
    rows = read_table(TABLE.read_text())
    missing = [p for p in DIGEST_PAIRS if Row(*p, "digest", "-") not in rows]
    assert not missing, f"no digest-carrying row for {missing}"


def test_the_committed_table_reads_both_architectures_of_every_homelab_tool() -> None:
    rows = [
        r
        for r in read_table(TABLE.read_text())
        if r.file == "hosts/packages/homelab-tools/package.yml"
    ]
    assert len(rows) == 16, "eight tools, two architectures"
    assert {r.digest for r in rows} == {"digest"}
    assert len({r.dep_name for r in rows}) == 8


def test_a_dep_with_no_value_and_untabled_managers_stay_out_of_the_table() -> None:
    report = _report(
        ("dockerfile", "Dockerfile", [{"depName": "curl", "datasource": "deb"}], ""),
        ("github-actions", "ci.yml", [{"depName": "x", "currentValue": "v1"}], ""),
        (
            "regex",
            "a.yml",
            [
                {"depName": "a/b", "datasource": "docker", "currentValue": "1"},
                {
                    "depName": "p/x",
                    "packageName": "p/all",
                    "datasource": "github-releases",
                    "currentValue": "1",
                },
            ],
            "",
        ),
    )
    rows = table_rows(deps_in(report))
    assert rows == [
        _row(),
        Row("regex", "a.yml", "p/x", "github-releases", "-", "p/all"),
    ]
    assert read_table(format_table(rows)) == rows


def test_a_matching_extract_is_clean() -> None:
    rows = [_row("digest")] * 2
    assert table_drift(rows, list(rows)) == []


def test_a_matchstring_that_lost_its_digest_is_flagged() -> None:
    expected = [_row("digest", "ci.yml", "goreleaser/nfpm")]
    actual = [_row("-", "ci.yml", "goreleaser/nfpm")]
    assert table_drift(expected, actual) == [
        "digest no longer captured:\tregex\tci.yml\tgoreleaser/nfpm\tdocker\t-"
    ]


def test_a_pin_digest_renovate_added_is_clean() -> None:
    """Renovate's pinDigest update gives a tag-only pin a digest; its own PR must stay green."""
    assert table_drift([_row("-")], [_row("digest")]) == []


def test_a_second_occurrence_the_extract_dropped_is_flagged() -> None:
    assert table_drift([_row(), _row()], [_row()]) == [
        "not extracted:\tregex\ta.yml\ta/b\tdocker\t-"
    ]


def test_a_dep_the_table_lacks_is_flagged() -> None:
    assert table_drift([], [_row()]) == [
        "not in the table:\tregex\ta.yml\ta/b\tdocker\t-"
    ]


# ── the autoReplaceStringTemplate ─────────────────────────────────────────────────────────


def test_a_template_reproducing_its_span_is_clean() -> None:
    span = f"_image: a/b:1.0@{DIGEST}"
    assert (
        broken_templates([_dep("x", span, "1.0", DIGEST, template=K8S_TEMPLATE)]) == []
    )


def test_a_template_without_its_literal_prefix_is_flagged() -> None:
    span = f"_image: a/b:1.0@{DIGEST}"
    template = K8S_TEMPLATE.replace("_image: ", "", 1)
    assert broken_templates([_dep("x", span, "1.0", DIGEST, template=template)]) == [
        f"x: {span!r} -> {'a/b:1.0@' + DIGEST!r}"
    ]


def test_the_k8s_template_pins_a_digest_onto_a_bare_tag_and_leaves_it_bare_without_one() -> (
    None
):
    values = {"depName": "a/b", "newValue": "1.0"}
    assert render_auto_replace(K8S_TEMPLATE, **values, newDigest=DIGEST) == (
        f"_image: a/b:1.0@{DIGEST}"
    )
    assert (
        render_auto_replace(K8S_TEMPLATE, **values, newDigest=None) == "_image: a/b:1.0"
    )


# ── a captured value cut short ────────────────────────────────────────────────────────────


def test_a_value_captured_whole_is_clean() -> None:
    assert truncated_values([_dep("x", "X_VERSION: 1.2.3", "1.2.3")]) == []
    assert truncated_values([_dep("x", "/download/v1.2/tool_1.2_linux", "1.2")]) == []


def test_a_value_cut_short_of_its_version_is_flagged() -> None:
    assert truncated_values([_dep("x", "X_VERSION: 1.2.3", "1.2")]) == [
        "x: a/b captured '1.2' out of 'X_VERSION: 1.2.3'"
    ]


def test_a_jellyfin_plugin_capturing_only_its_release_tag_is_clean() -> None:
    dep = Dep(
        "regex",
        "k8s/roles/jellyfin/defaults/main.yml",
        "vosmiic/jellyfin-ani-sync",
        "github-releases",
        "4.4",
        replace_string="ani-sync_4.4.0.0.zip",
    )
    assert truncated_values([dep]) == []


# ── pin lines no span covers ──────────────────────────────────────────────────────────────

ROLE = "k8s/roles/x/defaults/main.yml"
ROLE_TEXT = (
    f"x_k8s_image: a/b:1.0@{DIGEST}\n"
    "n8n_k8s_image: localhost:5000/n8n:1\n"
    "# renovate: datasource=github-releases depName=c/d\n"
    'x_version: "v1.2.3"\n'
)


def _uncovered(text: str, replace_strings: list[str], pinned=()) -> list[str]:
    read = {ROLE: text}.__getitem__
    lines = pin_lines([ROLE], read, pinned)
    return uncovered_pin_lines(lines, [_dep(ROLE, r) for r in replace_strings], read)


def test_pins_inside_some_span_are_clean() -> None:
    spans = [
        f"_image: a/b:1.0@{DIGEST}",
        '# renovate: datasource=github-releases depName=c/d\nx_version: "v1.2.3"',
    ]
    assert _uncovered(ROLE_TEXT, spans, [(ROLE, "x_version")]) == []


def test_an_unread_annotation_and_an_untracked_image_are_flagged() -> None:
    assert _uncovered(ROLE_TEXT, []) == [
        f"{ROLE}:1: an `_image:` key no manager reads: a/b:1.0@{DIGEST}",
        f"{ROLE}:3: a `# renovate:` annotation no manager reads: "
        "# renovate: datasource=github-releases depName=c/d",
    ]


def test_a_version_a_pinned_download_renders_from_is_flagged_when_untracked() -> None:
    text = 'x_version: "v1.2.3"\n'
    assert _uncovered(text, [], [(ROLE, "x_version")]) == [
        f"{ROLE}:1: the version a pinned download renders from no manager reads: v1.2.3"
    ]
    assert _uncovered(text, [], [(ROLE, "traefik_crds_k8s_version")]) == []


def test_a_control_plane_version_no_manager_reads_is_flagged() -> None:
    path = "k8s/roles/system-upgrade/defaults/main.yml"
    read = {path: "k3s_version: v1.35.1+k3s1\nk3s_window: 02:00\n"}.__getitem__
    lines = pin_lines([path], read)
    assert lines == [PinLine(path, 13, 25, "a control-plane version")]
    assert (
        uncovered_pin_lines(lines, [_dep(path, "k3s_version: v1.35.1+k3s1")], read)
        == []
    )


def test_the_pin_line_census_finds_its_known_members_in_the_tree() -> None:
    found = {
        (line.file, line.why)
        for line in pin_lines(tracked_files(), read_file, pinned_versions())
    }
    known = {
        ("k8s/roles/traefik/defaults/main.yml", "an `_image:` key"),
        ("inventory/group_vars/all.yml", "an `_image:` key"),
        ("hosts/components/registry-gc/defaults/main.yml", "an `_image:` key"),
        ("k8s/roles/system-upgrade/defaults/main.yml", "a control-plane version"),
        (".github/workflows/ci.yml", "a `# renovate:` annotation"),
        ("hosts/packages/homelab-tools/package.yml", "a `# renovate:` annotation"),
        (
            "k8s/roles/metallb/defaults/main.yml",
            "the version a pinned download renders from",
        ),
    }
    assert known <= found, sorted(known - found)


# ── release-download URLs ─────────────────────────────────────────────────────────────────

VALE = (
    "  curl -fsSL -o /tmp/vale.tar.gz \\\n"
    "    https://github.com/vale-cli/vale/releases/download/v3.18.0/"
    "vale_3.18.0_Linux_64-bit.tar.gz\n"
)


def _release(text: str, replace_strings: list[str], path: str = "ci.yml") -> list[str]:
    deps = [_dep(path, r) for r in replace_strings]
    return release_url_problems([path], deps, {path: text}.__getitem__)


def test_a_release_url_whose_every_version_is_in_a_span_is_clean() -> None:
    assert (
        _release(VALE, ["vale-cli/vale/releases/download/v3.18.0/", "vale_3.18.0_"])
        == []
    )


def test_a_tag_only_matchstring_is_flagged() -> None:
    assert _release(VALE, ["vale-cli/vale/releases/download/v3.18.0/"]) == [
        "ci.yml:2: release version 3.18.0 outside every manager's span"
    ]


def test_a_version_named_in_a_comment_is_clean() -> None:
    text = "# 3.18.0 is the last release that loads\n" + VALE
    assert (
        _release(text, ["vale-cli/vale/releases/download/v3.18.0/", "vale_3.18.0_"])
        == []
    )


def test_a_release_url_in_a_file_no_manager_reads_is_flagged() -> None:
    problems = release_url_problems(["vars.yml"], [], {"vars.yml": VALE}.__getitem__)
    assert problems == ["vars.yml: a release-download URL no manager extracts from"]
    assert (
        release_url_problems(["README.md"], [], {"README.md": VALE}.__getitem__) == []
    )


def test_the_release_url_census_finds_its_known_files_in_the_tree() -> None:
    known = {
        ".github/workflows/ci.yml",
        ".vale.ini",
        "k8s/roles/jellyfin/defaults/main.yml",
    }
    found = set(release_url_files(tracked_files(), read_file))
    assert known <= found, sorted(known - found)


# ── the Python annotation reader against Renovate's ─────────────────────────────────────

ANNOTATED = (
    "# renovate: datasource=github-releases depName=vendor/tool\n"
    'tool_version: "v1.2.3"\n'
)


def _annotated_dep(file: str = ROLE) -> Dep:
    return Dep(
        "regex",
        file,
        "vendor/tool",
        "github-releases",
        "v1.2.3",
        replace_string=ANNOTATED.rstrip(),
        dep_type="annotated-pin",
    )


def test_an_annotation_both_readers_see_is_clean() -> None:
    read = {ROLE: ANNOTATED}.__getitem__
    assert annotation_reader_drift([_annotated_dep()], [ROLE], read) == []


def test_an_annotation_only_renovate_reads_is_flagged() -> None:
    read = {ROLE: "tool_version: v1.2.3\n"}.__getitem__
    assert annotation_reader_drift([_annotated_dep()], [ROLE], read) == [
        f"{ROLE}: Renovate reads the annotated pin vendor/tool (github-releases); "
        "the Python reader does not"
    ]


def test_an_annotation_only_the_python_reader_reads_is_flagged() -> None:
    read = {ROLE: ANNOTATED}.__getitem__
    assert annotation_reader_drift([], [ROLE], read) == [
        f"{ROLE}: the Python annotation reader reads vendor/tool (github-releases); "
        "Renovate does not"
    ]


# ── which PRs run the extract ─────────────────────────────────────────────────────────────


def test_a_pr_touching_a_pinned_file_runs_the_extract() -> None:
    table = format_table(
        [Row("regex", ".vale.ini", "vale-cli/Google", "github-releases", "-", "-")]
    )
    assert needs_extract([".vale.ini"], table)
    assert needs_extract(["renovate.json"], table)
    assert needs_extract(["k8s/roles/new-role/defaults/main.yml"], table)
    assert needs_extract(["scripts/validate/renovate_extract.py"], table)
    assert needs_extract(["scripts/validate/asset_pins.py"], table)
    assert needs_extract(["scripts/lib/renovate_annotations.py"], table)


def test_a_pr_touching_only_unpinned_files_skips_the_extract() -> None:
    table = format_table(
        [Row("regex", ".vale.ini", "vale-cli/Google", "github-releases", "-", "-")]
    )
    assert not needs_extract(["docs/landing.md", "scripts/dev/fanout.py"], table)
    assert not needs_extract(
        [], table, added=["docs/new.md", "scripts/dev/new_tool.py"]
    )


def test_a_pr_adding_a_file_a_manager_could_read_runs_the_extract() -> None:
    table = format_table([])
    assert needs_extract([], table, added=["k8s/roles/new/templates/Dockerfile.j2"])
