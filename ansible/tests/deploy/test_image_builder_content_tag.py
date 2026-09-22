#!/usr/bin/env python3
"""Guards k8s/image-builder's content tag — the immutable second name every build pushes.

WHY IT EXISTS. Every built image was pushed to a mutable `:latest` alone, so "is the running pod
the image this commit describes" could only be answered by reading a registry digest
(post_tasks/k8s_image_drift_gate.yml). `<name>:sha-<12 hex>`, derived from the build inputs,
makes that a comparison of NAMES.

The failure this file is written against is not a build that breaks — it is a tag that stays
still while the image changes. That ships a stale image behind a fresh-looking name, which is
strictly worse than the mutable tag it was added to, and it reads green throughout. So every
test below is a pair: one input that must move the tag, and the identical input that must not.

The expression is read out of the role and evaluated through Ansible's own templar against real
files on disk, rather than restated here. A restated copy would keep passing after the role
changed, and a stubbed `lookup` would not prove the real lookups resolve at all.

Run: uv run pytest ansible/tests/deploy/test_image_builder_content_tag.py
"""

import re
import shutil

import pytest
from lib import yaml_fast
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from _helpers import ANSIBLE
from _helpers import load_tasks


ROLE = ANSIBLE / "roles" / "k8s" / "image-builder"
TASKS = ROLE / "tasks" / "main.yml"
CONTEXT_TPL = ROLE / "templates" / "context-configmap.yaml.j2"
BUILD_JOB_TPL = ROLE / "templates" / "build-job.yaml.j2"

FACT = "image_builder_content_tag"

# `sha-` + 12 hex. Both halves matter: the prefix is what registry-gc.sh's prune keys on to tell a
# content tag from `latest` or `buildcache`, and the length is what keeps the tag a legal Docker
# reference. A tag that stopped matching this would be pruned as an ordinary tag, or rejected by
# the registry outright.
TAG_RE = re.compile(r"^sha-[0-9a-f]{12}$")


def _tag_expression() -> str:
    """The Jinja template the role assigns to the content-tag fact, verbatim."""
    for task in load_tasks(TASKS):
        fact = task.get("ansible.builtin.set_fact") or {}
        if FACT in fact:
            return fact[FACT]
    pytest.fail(f"no set_fact assigns {FACT} in {TASKS}")


def _tag(dockerfile, context=None, context_files=None, role=ROLE, name="demo") -> str:
    """Evaluate the shipped expression against real paths, exactly as a deploy does.

    `set_basedir` is what puts `<role>/templates/` on the lookup's search path — the same place a
    role task finds a bare template name. Point `role` at a copy of the role to vary the
    templates themselves.
    """
    loader = DataLoader()
    loader.set_basedir(str(role))
    templar = Templar(
        loader=loader,
        variables={
            "image_builder_name": name,
            "k8s_namespace": "homelab",
            "image_builder_dockerfile": str(dockerfile),
            "image_builder_context": {str(k): v for k, v in (context or {}).items()},
            "image_builder_context_files": {
                str(k): v for k, v in (context_files or {}).items()
            },
        },
    )
    return str(templar.template(trust_as_template(_tag_expression()))).strip()


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
# choice: `data` alone means the render's prose and metadata must NOT move the tag, and these two
# tests are what say so in both directions.


def test_the_hash_reads_the_build_contexts_own_template():
    """Names the source, so a rewrite that hashes something else has to say so here."""
    assert CONTEXT_TPL.name in _tag_expression(), (
        f"the content tag no longer hashes {CONTEXT_TPL.name}'s render. It is the only artefact "
        "that is by construction every byte the build reads — anything else is a second list of "
        "inputs, which can silently omit one and hold the tag still across a change to it."
    )


def test_rewording_a_comment_in_that_template_does_not_move_the_tag(
    dockerfile, tmp_path
):
    """Why the hash takes `['data']` and not the whole render.

    That template is two-thirds prose. Hashing it whole would rebuild and re-roll all nine built
    images on a comment edit, which is most of the ~106s the build gate exists to save.
    """
    copy = tmp_path / "role"
    shutil.copytree(ROLE, copy)
    before = _tag(dockerfile, role=copy)
    tpl = copy / "templates" / CONTEXT_TPL.name
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


def _task_index(prefix: str) -> int:
    for i, task in enumerate(load_tasks(TASKS)):
        if task.get("name", "").startswith(prefix):
            return i
    pytest.fail(f"no task named {prefix!r} in {TASKS}")


def test_the_tag_is_computed_before_the_context_is_rendered():
    """build-job.yaml.j2 names the tag, so the fact has to exist before the render task runs."""
    assert _task_index("Compute the content tag") < _task_index(
        "Render the build context and job"
    ), (
        "the content tag is computed after the render, so build-job.yaml renders with an "
        "undefined tag and the build pushes a name nothing can reference."
    )


def test_the_tag_is_published_before_the_context_is_rendered():
    """A consumer reads the published map while rendering its own manifests, later in the play."""
    assert _task_index("Publish the content tag") < _task_index(
        "Render the build context and job"
    )


def test_the_build_pushes_both_names():
    """One exporter, two names — so both tags resolve to one digest.

    Pushing only the content tag would break every consumer still referencing `:latest`;
    pushing only `:latest` would leave the content tag absent and the gate rebuilding forever.
    """
    output = [
        line
        for line in BUILD_JOB_TPL.read_text().splitlines()
        if line.strip().startswith("- type=image")
    ]
    assert len(output) == 1, f"expected exactly one image exporter, found {output}"
    line = output[0]
    assert "{{ image_builder_content_tag }}" in line, (
        "the build's --output no longer names the content tag, so nothing pushes it and the "
        "registry read that gates the build 404s on every deploy."
    )
    assert "{{ image_builder_tag }}" in line, (
        "the build's --output no longer names the mutable tag, so every consumer still "
        "referencing `<name>:latest` fails to pull."
    )
    assert line.count('"') == 2, (
        "the two names must sit inside one quoted `name=` field: buildctl parses the exporter "
        "options as CSV, so an unquoted comma makes the second name an unknown option key."
    )


# --- the checks around the push ---------------------------------------------------------------


def _confirm_failed_when() -> str:
    for task in load_tasks(TASKS):
        if task.get("name", "").startswith("Confirm the registry now serves"):
            return str(task["failed_when"])
    pytest.fail(f"no confirm task in {TASKS}")


def _confirms_ok(tags, *, no_mutate=False) -> bool:
    """True when the confirm step passes for a registry serving `tags`."""
    templar = Templar(
        loader=DataLoader(),
        variables={
            "k8s_no_mutate": no_mutate,
            "image_builder_tag": "latest",
            "image_builder_content_tag": "sha-abcdef012345",
            "image_builder_tags": {"json": {"tags": list(tags)}},
        },
    )
    rendered = templar.template(
        trust_as_template("{{ " + _confirm_failed_when() + " }}")
    )
    return str(rendered) == "False"


def test_the_confirm_step_passes_when_both_names_were_pushed():
    assert _confirms_ok(["latest", "sha-abcdef012345", "buildcache"])


def test_the_confirm_step_fails_when_only_the_mutable_tag_was_pushed():
    """The red-proof half: a `--output` that stopped emitting the second name must be caught.

    Without this the build reports success, `:latest` moves, and the content tag every consumer
    references is absent — which surfaces as an ImagePullBackOff on the next pod, not here.
    """
    assert not _confirms_ok(["latest", "buildcache"])


def test_the_confirm_step_still_fails_when_the_mutable_tag_is_missing():
    """Unchanged from before the content tag: every unconverted consumer pulls `:latest`."""
    assert not _confirms_ok(["sha-abcdef012345"])


def test_a_dry_run_of_an_edited_context_does_not_fail_the_confirm_step():
    """Nothing pushed under --check, so a changed context legitimately has no content tag yet.

    Requiring it there would fail every dry run of an image edit — the one workflow a dry run of
    an image edit exists for.
    """
    assert _confirms_ok(["latest"], no_mutate=True)


def test_the_play_resets_the_published_tag_map():
    """Facts persist across plays and resumed runs, and this one names a registry tag.

    A leftover entry from an earlier play names a tag the registry may no longer serve. A
    `--tags` run that skips the building role would render it into a consumer's manifest and
    fail to pull, where an empty map falls back to the mutable alias.
    """
    play = yaml_fast.safe_load((ANSIBLE / "deploy.yml").read_text())
    resets = [
        t["ansible.builtin.set_fact"]
        for p in play
        for t in p.get("pre_tasks", [])
        if isinstance(t, dict) and "ansible.builtin.set_fact" in t
    ]
    assert any("k8s_built_image_tags" in facts for facts in resets), (
        "deploy.yml must reset k8s_built_image_tags in pre_tasks, alongside k8s_built_images. "
        "Without it a --tags run inherits content tags this play never pushed."
    )
