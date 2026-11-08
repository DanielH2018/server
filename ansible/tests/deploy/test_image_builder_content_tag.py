#!/usr/bin/env python3
"""Guards k8s/image-builder's content tag — the immutable second name every build pushes.

WHY IT EXISTS. A mutable `:latest` alone can answer "is the running pod the image this commit
describes" only by reading a registry digest.
`<name>:sha-<12 hex>`, derived from the build inputs, makes that a comparison of NAMES.

The failure this file is written against is not a build that breaks — it is a tag that stays
still while the image changes. That ships a stale image behind a fresh-looking name, which is
strictly worse than the mutable tag, and it reads green throughout. So every
test below is a pair: one input that must move the tag, and the identical input that must not.

`derived.content_tag` is the rule under test. Each tag is checked against the role's expression,
evaluated through Ansible's own templar against real files on disk, because a stubbed `lookup`
would not prove the real lookups resolve at all.

Run: uv run pytest ansible/tests/deploy/test_image_builder_content_tag.py
"""

import re
import shutil
from typing import cast

import pytest
from _helpers import ANSIBLE
from _image_builder_tag import (
    CONTEXT_DATA_EXPRESSION,
    templar_content_tag,
    templar_value,
)
from _k8s_render import render_role_template
from renderer.renderer_lib.derived import content_tag


ROLE = ANSIBLE / "roles" / "k8s" / "image-builder"
CONTEXT_TPL = ROLE / "templates" / "context-configmap.yaml.j2"
BUILD_JOB_TPL = "build-job.yaml.j2"


# `sha-` + 12 hex. Both halves matter: the prefix is what registry-gc.sh's prune keys on to tell a
# content tag from `latest` or `buildcache`, and the length is what keeps the tag a legal Docker
# reference. A tag that stopped matching this would be pruned as an ordinary tag, or rejected by
# the registry outright.
TAG_RE = re.compile(r"^sha-[0-9a-f]{12}$")


def _tag(dockerfile, context=None, context_files=None, role=ROLE, name="demo") -> str:
    """`derived.content_tag` over the real context, checked against Ansible's evaluation of it.

    Point `role` at a copy of the role to vary the templates themselves.
    """
    variables = {
        "image_builder_name": name,
        "k8s_namespace": "homelab",
        "image_builder_dockerfile": str(dockerfile),
        "image_builder_context": {str(k): v for k, v in (context or {}).items()},
        "image_builder_context_files": {
            str(k): v for k, v in (context_files or {}).items()
        },
    }
    data = cast(dict, templar_value(CONTEXT_DATA_EXPRESSION, variables, role))
    tag = content_tag(data)
    assert tag == templar_content_tag(variables, role)
    return tag


def _write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body)
    return path


@pytest.fixture
def dockerfile(tmp_path):
    return _write(tmp_path, "Dockerfile.j2", "FROM alpine:3.24\nRUN true\n")


# --- the tag itself ---------------------------------------------------------------------------


def test_the_tag_is_a_registry_safe_sha_prefix(dockerfile):
    """Non-vacuity. Every pair below compares two tags, and two empty strings compare equal."""
    tag = _tag(dockerfile)
    assert TAG_RE.match(tag), (
        f"the content tag rendered as {tag!r}, which is not `sha-` + 12 hex. registry-gc.sh's "
        "prune keys on exactly that pattern to tell a content tag from `latest` and "
        "`buildcache` — a tag outside it is either pruned as an ordinary tag or refused by the "
        "registry as an illegal reference."
    )


def test_identical_inputs_give_the_same_tag(dockerfile, tmp_path):
    """The point of a content tag: the same bytes must not produce a new name.

    Without this, every deploy renames the image, the consumer's Deployment spec changes, and
    every workload rolls on every run — the opposite of what the build gate exists to save.
    """
    ctx = {_write(tmp_path, "extensions.sh.j2", "echo hi\n"): "extensions.sh"}
    files = {_write(tmp_path, "app.py", "print(1)\n"): "app.py"}
    assert _tag(dockerfile, ctx, files) == _tag(dockerfile, ctx, files)


def test_a_changed_dockerfile_changes_the_tag(dockerfile, tmp_path):
    before = _tag(dockerfile)
    dockerfile.write_text("FROM alpine:3.24\nRUN false\n")
    assert _tag(dockerfile) != before


def test_a_changed_context_template_changes_the_tag(dockerfile, tmp_path):
    """The `image_builder_context` arm — a template rendered into the context."""
    src = _write(tmp_path, "extensions.sh.j2", "echo hi\n")
    before = _tag(dockerfile, {src: "extensions.sh"})
    src.write_text("echo bye\n")
    assert _tag(dockerfile, {src: "extensions.sh"}) != before


def test_a_changed_context_file_changes_the_tag(dockerfile, tmp_path):
    """The `image_builder_context_files` arm, and the one most likely to be left out.

    homelab-mcp's app.py arrives this way — embedded with `lookup('file')` because Jinja must
    not touch the PromQL braces inside it. A hash over the Dockerfile alone holds the tag still
    across an app.py rewrite, which is the stale-image-behind-a-fresh-name failure.
    """
    src = _write(tmp_path, "app.py", "print(1)\n")
    before = _tag(dockerfile, None, {src: "app.py"})
    src.write_text("print(2)\n")
    assert _tag(dockerfile, None, {src: "app.py"}) != before


def test_a_renamed_context_destination_changes_the_tag(dockerfile, tmp_path):
    """The dest is where the Dockerfile's COPY finds the file, so it is an input too."""
    src = _write(tmp_path, "extensions.sh.j2", "echo hi\n")
    assert _tag(dockerfile, {src: "extensions.sh"}) != _tag(
        dockerfile, {src: "setup.sh"}
    )


def test_a_caller_with_no_context_at_all_still_gets_a_tag(dockerfile):
    """n8n and nut pass a Dockerfile and nothing else — the empty-lookup path."""
    assert TAG_RE.match(_tag(dockerfile, {}, {}))


# --- what the hash reads, and what it deliberately ignores -------------------------------------
#
# The tag hashes the `data` of context-configmap.yaml.j2's own render, so there is no second list
# of inputs that can fall out of step with the build context. The pair below is the cost of that
# choice: `data` alone means the render's prose and metadata must NOT move the tag, and the two
# tests below say so.


def test_rewording_a_comment_in_that_template_does_not_move_the_tag(
    dockerfile, tmp_path
):
    """Why the hash takes `['data']` and not the whole render.

    That template is two-thirds prose. Hashing it whole would rebuild and re-roll every built
    image on a comment edit, which is most of the ~106s the build gate exists to save.
    """
    copy = tmp_path / "role"
    shutil.copytree(ROLE, copy)
    before = _tag(dockerfile, role=copy)
    tpl = tmp_path / "role" / "templates" / CONTEXT_TPL.name
    tpl.write_text(
        "# an added comment, changing nothing the build reads\n" + tpl.read_text()
    )
    assert _tag(dockerfile, role=copy) == before


def test_the_image_name_does_not_move_the_tag(dockerfile):
    """The tag addresses CONTENT; the repository name carries which image it is.

    Two callers building byte-identical contexts share a content tag, in different repositories.
    Folding the name in would look tidier and would mean a rename rebuilds an unchanged image.
    """
    assert _tag(dockerfile, name="demo") == _tag(dockerfile, name="other")


# --- ordering and the push -------------------------------------------------------------------


def test_the_build_pushes_both_names():
    """One exporter, two names — so both tags resolve to one digest.

    Pushing only the content tag would break every consumer still referencing `:latest`;
    pushing only `:latest` would leave the content tag absent and the gate rebuilding forever.
    """
    rendered = render_role_template(
        "image-builder",
        BUILD_JOB_TPL,
        {
            "image_builder_name": "demo",
            "image_builder_content_tag": "sha-c0ffee",
            "image_builder_tag": "mutable",
        },
    )
    output = [
        line
        for line in rendered.splitlines()
        if line.strip().startswith("- type=image")
    ]
    assert len(output) == 1, f"expected exactly one image exporter, found {output}"
    line = output[0]
    assert "/demo:sha-c0ffee" in line, (
        "the build's --output no longer names the content tag, so nothing pushes it and the "
        "registry read that gates the build 404s on every deploy."
    )
    assert "/demo:mutable" in line, (
        "the build's --output no longer names the mutable tag, so every consumer still "
        "referencing `<name>:latest` fails to pull."
    )
    assert line.count('"') == 2, (
        "the two names must sit inside one quoted `name=` field: buildctl parses the exporter "
        "options as CSV, so an unquoted comma makes the second name an unknown option key."
    )


# --- the checks around the push ---------------------------------------------------------------
