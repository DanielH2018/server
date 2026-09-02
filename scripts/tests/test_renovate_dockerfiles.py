"""Guards on the images this repo builds itself:

every Dockerfile is Renovate-visible, no base image floats, and the pins that exist twice by design
move in lockstep.

A Dockerfile Renovate cannot see ages silently, and a `FROM` without a tag or digest is rebuilt
against whatever upstream pushed last. The lockstep guards cover n8n (two Dockerfiles),
shellcheck-py (prek.toml and pyproject.toml) and the Python version (`.python-version` and both
workflows), each of which Renovate bumps as separate PRs.

Run: uv run pytest scripts/tests/test_renovate_dockerfiles.py
"""

from __future__ import annotations

import re


from _renovate import (
    _REPO,
)


# Renovate's BUILT-IN dockerfile manager's default managerFilePatterns, copied verbatim from source
# (lib/modules/manager/dockerfile/index.ts, verified against upstream 2026-07-13). The fleet's
# Dockerfile base pins are tracked ONLY by that manager (no custom manager covers them), so a build
# file renamed/added outside these shapes drops out of update tracking with no signal. NB the 2nd
# pattern's `[^/]*$` matches suffixed names too (`Dockerfile-runners.j2` IS visible), so this guard
# reflects exactly what Renovate scans — the earlier `[Cc]ontain` was a typo (matched a nonexistent
# `Containfile`, missed a real `Containerfile`); upstream is `[Cc]ontainer`.
DOCKERFILE_MANAGER_FILE_RES = [
    re.compile(r"(^|/|\.)([Dd]ocker|[Cc]ontainer)file$"),
    re.compile(r"(^|/)([Dd]ocker|[Cc]ontainer)file[^/]*$"),
]


def test_every_dockerfile_is_renovate_visible(tracked: list[str]) -> None:
    """Every FROM-bearing build file must sit where Renovate's dockerfile manager looks.

    The compose-template guard above covers `image:` lines; this is its sibling for built
    images. Discovery is by CONTENT (any tracked ansible/ file with a FROM line), not by
    name, so the check doesn't share the blind spot it guards against.

    Whether the FROM carries a version is a separate question, and its own test below —
    this one is purely about file NAMING, so a build file renamed out of the manager's
    filePatterns still fails here even when its pin is explicit.
    """
    from_re = re.compile(r"^FROM\s+\S+", re.MULTILINE)
    build_files = [
        f
        for f in tracked
        if f.startswith("ansible/")
        and not f.endswith(".md")
        and from_re.search((_REPO / f).read_text(errors="ignore"))
    ]
    assert build_files, (
        "no FROM-bearing build files found under ansible/ (discovery drifted?)"
    )
    escaped = [
        f
        for f in build_files
        if not any(r.search(f) for r in DOCKERFILE_MANAGER_FILE_RES)
    ]
    assert not escaped, (
        "Build file(s) with a FROM line that Renovate's dockerfile manager will NOT scan "
        "(name doesn't match its filePatterns) — their base-image pins will age silently:\n"
        + "\n".join(escaped)
    )


# A FROM line's image reference, plus the stage name a multi-stage build gives it.
_FROM_RE = re.compile(
    r"^FROM\s+(?P<ref>\S+)(?:\s+[Aa][Ss]\s+(?P<stage>\S+))?", re.MULTILINE
)


def test_no_built_image_floats_on_an_unpinned_base(tracked: list[str]) -> None:
    """No FROM may float: every base needs an explicit version tag or a digest.

    This test used to be the exemption it now forbids. The sibling above blessed untagged
    and `:latest` FROMs as "the deliberate rolling tier (build-on-recreate semantics)" —
    which was true only under Docker Compose, where `build: always` plus the weekly
    roles/containers/common/tasks/redeploy_cron.yml redeploy forced the rebuild that picked
    up new base layers. The k3s migration archived that cron's last caller on 2026-08-14 and
    put nothing in its place, so from then on a floating FROM had NO updater whatsoever:
    Renovate has no version to bump, so no PR is raised, so no commit lands, so gitops never
    ticks and no rebuild ever runs. Three images (n8n, n8n-runners, code-server) drifted that
    way until 2026-08-19 — n8n stuck on 2.34.6 while upstream shipped 2.35.4.

    So the rule inverts: an explicit tag is what makes the base a tracked dependency with a
    PR, CI and a review, exactly like every pulled image in the fleet. A `:latest@sha256:...`
    digest pin is accepted — that is a real, Renovate-bumpable pin, and it is the shape the
    k8s roles already use for mutable-tag upstreams.

    Multi-stage internal references (`FROM builder`) are skipped: a stage name declared
    earlier in the same file is not an upstream image and has nothing to pin.
    """
    build_files = [
        f
        for f in tracked
        if f.startswith("ansible/")
        and not f.endswith(".md")
        and not f.startswith("ansible/roles/containers/archive/")
        and _FROM_RE.search((_REPO / f).read_text(errors="ignore"))
    ]
    assert build_files, (
        "no FROM-bearing build files found under ansible/ (discovery drifted?)"
    )
    floating = []
    for f in build_files:
        stages: set[str] = set()
        for m in _FROM_RE.finditer((_REPO / f).read_text(errors="ignore")):
            ref = m.group("ref")
            if m.group("stage"):
                stages.add(m.group("stage").lower())
            # An earlier stage in the same file, not an upstream image.
            if ref.lower() in stages:
                continue
            # A digest is a pin in its own right, whatever the tag says.
            if "@sha256:" in ref:
                continue
            # Strip the registry host before looking for a tag, so the `:5000` in a
            # `localhost:5000/foo` host:port is never mistaken for one.
            tag = ref.rsplit("/", 1)[-1].partition(":")[2]
            # A tag naming a LINE rather than a release still moves — `python:3.14-slim`
            # re-points at every 3.14.x, `debian:bookworm-slim` at every point release. Renovate
            # can only offer a TAG change for those (3.14 -> 3.15), never the movement inside
            # one, so the tag reads pinned while the base drifts. A release tag is the case
            # where the tag alone suffices, and two dots is what separates the two:
            # `2.35.4` and `4.133.0-ls358` are releases, `3.14-slim` / `3.22` / `bookworm-slim`
            # are lines. A line tag must therefore carry a digest as well.
            if not tag or tag == "latest" or tag.count(".") < 2:
                floating.append(f"{f}: FROM {ref}")
    assert not floating, (
        "Build file(s) whose base image can still move. Renovate cannot bump a tag with no "
        "version in it, nor the movement inside a line tag, so these have no update path — "
        "not a PR, not an alert, not a rebuild. Pin an exact release tag, or keep the line "
        "tag and add the @sha256 digest beside it:\n" + "\n".join(floating)
    )


def test_n8n_base_pins_in_lockstep() -> None:
    """n8n's app and task-runner base pins must name the same tag.

    The two images are version-coupled: the runners execute Code-node tasks on behalf of the
    n8n they serve, and the runners Dockerfile pins pnpm to the base's own store version. A
    skew does not fail the build — it surfaces at RUNTIME as every Code-node workflow timing
    out while both pods stay Ready, which is precisely the 2026-08-06 `@n8n/di` failure that
    file's header records. Renovate groups them into one PR (the "n8n (lockstep: app + task
    runners)" packageRule); this asserts the coupling actually held.

    Compares the TAG and stops at the `@`. Both pins are `stable@sha256:...`, and their
    digests necessarily DIFFER — they are two different images. What has to match is the
    channel each one follows, because that is what decides they describe the same release.
    The check reads the same way for a plain version pin, so it survives a move back to one.

    The same shape as test_shellcheck_py_pins_in_lockstep below, and for the same reason: a
    grouping rule expresses intent, only a test enforces it.
    """
    root = _REPO / "ansible/roles/k8s/n8n-images/templates"
    app = re.search(
        r"^FROM\s+n8nio/n8n:([^@\s]+)",
        (root / "Dockerfile.j2").read_text(),
        re.MULTILINE,
    )
    runners = re.search(
        r"^FROM\s+n8nio/runners:([^@\s]+)",
        (root / "Dockerfile-runners.j2").read_text(),
        re.MULTILINE,
    )
    assert app, "no `FROM n8nio/n8n:<tag>` in Dockerfile.j2"
    assert runners, "no `FROM n8nio/runners:<tag>` in Dockerfile-runners.j2"
    assert app.group(1) == runners.group(1), (
        f"n8n base pins have drifted apart: app follows {app.group(1)}, runners follows "
        f"{runners.group(1)}. They are version-coupled — a skew surfaces as Code-node "
        "workflows failing at runtime, not as a build error. Move both together."
    )


def test_shellcheck_py_pins_in_lockstep() -> None:
    """prek.toml's shellcheck-py rev and pyproject.toml's `shellcheck-py==` pin must match.

    The two pins back DIFFERENT execution paths of the same tool — the prek hook env lints
    committed shell scripts, the pyproject dev dep lints RENDERED .sh.j2 output via
    validate_shell_templates — so a version skew means the two gates disagree about the same
    code. They are tracked by different Renovate datasources (github-tags vs pypi); a
    packageRule groups them into one PR, and this asserts that coupling actually held.
    """
    prek = (_REPO / "prek.toml").read_text()
    pyproject = (_REPO / "pyproject.toml").read_text()
    rev = re.search(
        r'repo = "https://github\.com/shellcheck-py/shellcheck-py"\s+rev = "v([^"]+)"',
        prek,
    )
    assert rev, "shellcheck-py repo/rev pin not found in prek.toml"
    pin = re.search(r'"shellcheck-py==([^"]+)"', pyproject)
    assert pin, "shellcheck-py== pin not found in pyproject.toml"
    assert rev.group(1) == pin.group(1), (
        f"shellcheck-py pins drifted: prek.toml rev v{rev.group(1)} vs "
        f"pyproject.toml =={pin.group(1)} — bump both together (they render/lint the same shell)."
    )


def test_python_version_pins_in_lockstep() -> None:
    """ci.yml, image-smoke.yml, and .python-version must pin the same Python minor version.

    A single Renovate customManager scans every workflow file (renovate.json), so one bump PR is
    meant to edit both `python-version:` pins together; nothing else asserts the coupling actually
    held. A skew would run the scripts suite under one interpreter in CI and boot-smoke changed
    images under another — a silent test/runtime mismatch. `.python-version` (the interpreter host
    `uv run` selects) is tracked by a SEPARATE Renovate manager (built-in pyenv) whose PR does NOT
    automerge, so it can lag the workflow bump — leaving the host on 3.N while CI moves to 3.N+1.
    Compared on major.minor (pyenv may carry a patch the workflow pin omits). Mirrors the
    shellcheck-py / portainer lockstep tests above.
    """
    ci = (_REPO / ".github/workflows/ci.yml").read_text()
    smoke = (_REPO / ".github/workflows/image-smoke.yml").read_text()
    dotver = (_REPO / ".python-version").read_text().strip()
    c = re.search(r'python-version:\s*"([^"]+)"', ci)
    s = re.search(r'python-version:\s*"([^"]+)"', smoke)
    assert c, "python-version pin not found in ci.yml"
    assert s, "python-version pin not found in image-smoke.yml"
    assert c.group(1) == s.group(1), (
        f"python-version pins drifted: ci.yml {c.group(1)} vs image-smoke.yml {s.group(1)} — bump "
        f"both together (they must run the scripts suite and image smoke on the same interpreter)."
    )

    def _minor(v: str) -> str:
        return ".".join(v.split(".")[:2])

    assert _minor(dotver) == _minor(c.group(1)), (
        f"python-version drifted: .python-version {dotver} vs the workflows' {c.group(1)} — bump "
        f"the pyenv .python-version to match (its Renovate PR doesn't automerge, so it can lag)."
    )
