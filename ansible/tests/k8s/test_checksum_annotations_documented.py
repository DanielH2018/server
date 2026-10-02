"""Every `checksum/<name>` a role's CLAUDE.md names must exist in that role's templates.

A doc can promise an annotation the manifests do not have, such as `checksum/config` where the
manifest has `checksum/check-script`. That sends someone debugging a pod that will not restart
to look for a mechanism that was never there.

The census reads each role's manifest templates out of `_k8s_render.rendered_texts`, so a name
the shared `checksum_annotation()` macro builds — or one a Jinja expression assembles — arrives
expanded (#3225). Only the files no render covers are read as source: the `*.yaml`/`*.yml` task
files, and the four kinds of template `_render_texts` skips (`SOURCE_FALLBACK_MEMBERS` names
one of each).

Run: uv run pytest ansible/tests/k8s/test_checksum_annotations_documented.py
"""

import functools
import re
from pathlib import Path

import pytest
from _helpers import REPO
from _k8s_render import rendered_texts
from _role_census import role_dirs


K8S_ROLES = REPO / "ansible" / "roles" / "k8s"

# `checksum/foo` in prose, including inside backticks. The trailing char class stops at the
# closing backtick or punctuation rather than swallowing the rest of the sentence.
CHECKSUM_RE = re.compile(r"checksum/([a-zA-Z0-9._-]+)")

# `checksum_annotation('foo', ...)` — the shared macro (ansible/templates/checksum-
# annotation.yml.j2) that monitor-bridge, autofix-bridge, n8n, game-stats and observability
# call instead of writing `checksum/foo:` literally. Scoped to the SOURCE half of the census:
# the render expands the macro into the literal `checksum/foo` that CHECKSUM_RE already
# matches, so a rendered template needs this pattern for nothing. A template the render does
# not reach — a nested `templates/config/*.j2`, a role with no `containers_list` entry — can
# still call the macro, and there the call is all the census gets to see.
MACRO_CALL_RE = re.compile(r"checksum_annotation\(\s*['\"]([a-zA-Z0-9._-]+)['\"]")


@functools.cache
def _rendered_by_role() -> dict[str, dict[str, str]]:
    """Every rendered manifest text, keyed by role name then template name.

    The names a role renders include the shared defaults `k8s/manifests` renders on its behalf
    (`service.yaml.j2` for 25 roles), which have no file in the role at all. That is extra
    coverage, and it is also why the fallback below keys on the template NAME rather than on
    the path.
    """
    by_role: dict[str, dict[str, str]] = {}
    for role, template, text in rendered_texts():
        by_role.setdefault(role, {})[template] = text
    return by_role


# The four kinds of `*.j2` `_k8s_render._render_texts` does not render, each with why. A
# template of any other shape that falls back is the drift this file's fallback test catches:
# it means a manifest template dropped out of the render and the census silently went back to
# scanning `{{ ... }}`.
#
# # DECIDED: categories with one pinned member each, not a literal list of all ~40 files. The
# set itself churns with every new `templates/config/*.j2`, which carries no annotation and
# tells a reader nothing; the category and its named member are what has to stay true.
SOURCE_FALLBACK_REASONS = {
    "unrendered-role": (
        "the role has no `containers_list` entry or is caller-rendered, so no render reaches "
        "any of its templates"
    ),
    "shell": "a `*.sh.j2` renders to a script, not to a manifest, and `_render_texts` skips it",
    "build": "a `Dockerfile*` feeds an image build; `rendered_build_texts` renders those",
    "nested": (
        "a template below `templates/`, which `_render_texts` globs exactly one level deep"
    ),
}

# One template per category the derivation must still find there, so a category whose detection
# breaks fails as a missing member rather than passing over an empty set.
SOURCE_FALLBACK_MEMBERS = {
    "image-builder/templates/build-job.yaml.j2": "unrendered-role",
    "qbittorrent/templates/prefs-check.sh.j2": "shell",
    "code-server/templates/Dockerfile.j2": "build",
    "homepage/templates/config/services.yaml.j2": "nested",
}


def _fallback_category(role: Path, path: Path) -> str | None:
    """Why `path` is read as source, or None when the render should have covered it."""
    rendered = _rendered_by_role().get(role.name, {})
    if not rendered:
        return "unrendered-role"
    if path.name in rendered:
        return None
    if path.name.endswith(".sh.j2"):
        return "shell"
    if path.name.startswith("Dockerfile"):
        return "build"
    if path.parent != role / "templates":
        return "nested"
    return None


def _source_fallbacks() -> dict[str, str | None]:
    """Every `*.j2` the census reads as source, as `<role>/<path>` -> category or None."""
    return {
        f"{role.name}/{path.relative_to(role)}": _fallback_category(role, path)
        for role in role_dirs()
        for path in sorted(role.rglob("*.j2"))
        if path.name not in _rendered_by_role().get(role.name, {})
    }


def _role_dirs():
    return sorted(d for d in role_dirs() if (d / "CLAUDE.md").is_file())


def _annotations_in_templates(role: Path) -> set[str]:
    """Every `checksum/<name>` `role` defines, read from its renders where one exists."""
    found = set()
    for text in _rendered_by_role().get(role.name, {}).values():
        found |= set(CHECKSUM_RE.findall(text))
    sources = (
        [
            path
            for path in role.rglob("*.j2")
            if path.name not in _rendered_by_role().get(role.name, {})
        ]
        + list(role.rglob("*.yaml"))
        + list(role.rglob("*.yml"))
    )
    for path in sources:
        text = path.read_text(errors="replace")
        found |= set(CHECKSUM_RE.findall(text))
        found |= set(MACRO_CALL_RE.findall(text))
    return found


@pytest.mark.parametrize("role", _role_dirs(), ids=lambda p: p.name)
def test_claude_md_checksum_names_exist_in_the_role(role: Path):
    documented = set(
        CHECKSUM_RE.findall((role / "CLAUDE.md").read_text(errors="replace"))
    )
    if not documented:
        pytest.skip("role's CLAUDE.md names no checksum annotation")
    actual = _annotations_in_templates(role)
    missing = documented - actual
    assert not missing, (
        "%s/CLAUDE.md names checksum annotation(s) %s that appear in no template in that role; "
        "the role actually defines %s"
        % (role.name, sorted(missing), sorted(actual) or "none")
    )


def test_root_claude_md_checksum_names_exist_somewhere():
    """The repo-root CLAUDE.md is not scoped to one role, so its names are checked against the
    whole k8s tree."""
    documented = set(
        CHECKSUM_RE.findall((REPO / "CLAUDE.md").read_text(errors="replace"))
    )
    if not documented:
        pytest.skip("root CLAUDE.md names no checksum annotation")
    actual = set()
    for role in role_dirs():
        actual |= _annotations_in_templates(role)
    missing = documented - actual
    assert not missing, (
        "repo-root CLAUDE.md names checksum annotation(s) %s that exist in no k8s role template"
        % sorted(missing)
    )


def test_the_render_carries_the_macro_built_annotation():
    """The widening, as a named member: the macro's own output is visible on the render.

    `monitor-bridge/templates/deployment.yaml.j2` writes
    `checksum_annotation('check-script', ...)` and the literal `checksum/check-script` appears
    nowhere in its source, so this name is found only because the census reads the render. It
    is the whole reason the macro pattern no longer has to stand in for the render.
    """
    role = K8S_ROLES / "monitor-bridge"
    rendered = _rendered_by_role().get("monitor-bridge", {})
    assert "deployment.yaml.j2" in rendered, (
        "monitor-bridge's Deployment is not among the rendered manifests, so this guard's "
        f"corpus is empty: {sorted(rendered)}"
    )
    assert "check-script" not in CHECKSUM_RE.findall(
        (role / "templates" / "deployment.yaml.j2").read_text()
    ), (
        "the source now spells the annotation out, so it no longer proves the render is read"
    )
    assert "check-script" in set(CHECKSUM_RE.findall(rendered["deployment.yaml.j2"])), (
        "the render of monitor-bridge's Deployment no longer carries checksum/check-script; "
        "the macro's output is what this census reads"
    )


def test_every_source_fallback_is_a_template_no_render_covers():
    """The source half of the census reads only the four kinds of template `_render_texts` skips.

    A manifest template of any other shape reading as a fallback means it dropped out of the
    render, and the census went back to scanning its Jinja — where `checksum/{{ name }}`
    matches nothing and the role reads as defining no annotation at all.
    """
    unexplained = sorted(
        name for name, category in _source_fallbacks().items() if category is None
    )
    assert unexplained == [], (
        "these templates are read as source but match no fallback category "
        f"({sorted(SOURCE_FALLBACK_REASONS)}): {unexplained}"
    )


@pytest.mark.parametrize("name,category", sorted(SOURCE_FALLBACK_MEMBERS.items()))
def test_the_named_fallback_members_still_fall_back(name: str, category: str):
    """Each category keeps a real member, so none of them is a branch over nothing."""
    fallbacks = _source_fallbacks()
    assert name in fallbacks, (
        f"{name} no longer falls back to source, so the {category!r} category "
        f"({SOURCE_FALLBACK_REASONS[category]}) has lost its named member"
    )
    assert fallbacks[name] == category, (
        f"{name} now falls back as {fallbacks[name]!r}, not {category!r}"
    )


def test_a_manifest_template_dropping_out_of_the_render_reads_as_unexplained():
    """The red half of the fallback rule: a top-level manifest template that is not rendered.

    `_fallback_category` is asked about a `templates/*.yaml.j2` of a role the render DOES reach,
    under a name that render does not carry. That is what a template dropping out of
    `_render_texts` looks like, and it has to come back as None so the fallback test fails
    naming it rather than quietly scanning Jinja.
    """
    role = K8S_ROLES / "monitor-bridge"
    assert (
        _fallback_category(role, role / "templates" / "not-in-the-render.yaml.j2")
        is None
    )
