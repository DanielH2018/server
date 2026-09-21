"""The auto-approve-readonly.py half of the shared adversarial command corpus.

Two independent implementations judge Bash commands here: this repo's
auto-approve-readonly.py (python), and cmdparse.sh in the chezmoi dotfiles repo (bash),
which feeds the PreToolUse/PermissionRequest guards. Both were fixed for the *same*
bypass — a newline separates two commands, but shlex and a flattening regex each treat it
as plain whitespace and merge the pair — independently, in two languages, in two repos.
See the `shlex treats it as plain whitespace` comment in auto-approve-readonly.py's
classify(), and cmdparse.sh's own header.

They are meant to stay separate: cmdparse.sh documents why it is bash and not python (hot
path on every Bash call, and a missing interpreter would be a new fail-open surface). What
they must not do is disagree about which strings are dangerous. The shared corpus is the
guard against that. This module asserts the `readonly` field; the chezmoi repo's
tests/hooks/command-vectors.test.js asserts the `cmdparse` field of the same vectors.

The corpus is VENDORED at `fixtures/command-vectors.json`, so the case set is a fact about
this tree: CI and every checkout collect the same tests (#2159). Until then the default was
the real dotfiles checkout under `~/.local/share/chezmoi`, which CI does not have and which
differs per machine and per `chezmoi apply` state — the classify tests skipped in CI and
collected a different count on every host. The dotfiles copy is still the one the other
side edits, so `test_the_vendored_corpus_matches_the_dotfiles_copy` diffs the two wherever
the dotfiles checkout exists — the same shape as the claude_guard stand-in diff in
test_claude_guard_import.py — and `prek run` runs it before every commit from such a host.
Point COMMAND_VECTORS at another copy to replay against it instead.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent

VENDORED = Path(__file__).resolve().parent / "fixtures" / "command-vectors.json"
_DOTFILES_COPY = (
    Path.home() / ".local/share/chezmoi/tests/fixtures/command-vectors.json"
)
# `or`, not a default argument: `COMMAND_VECTORS= uv run pytest ...` is the documented way to
# say "the vendored one", and an empty value must not resolve to `Path("")`.
FIXTURE = Path(os.environ.get("COMMAND_VECTORS") or VENDORED)

# The count vendored on 2026-09-21. A vendored file that shrinks fails here instead of
# collecting fewer tests; raise this when a vector is added.
_VENDORED_VECTOR_COUNT = 15


def _load_classifier():
    sys.path.insert(0, str(HOOKS))  # auto-approve-readonly.py imports _hook_common
    spec = importlib.util.spec_from_file_location(
        "auto_approve_readonly", HOOKS / "auto-approve-readonly.py"
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify


def _ids(vectors):
    return [v["name"] for v in vectors]


# Collected at import time so each vector is its own test case rather than one opaque loop.
# A corpus that fails to load yields an empty list, and test_corpus_is_readable is what
# reports that — an empty parametrize would otherwise pass as zero tests.
try:
    VECTORS = json.loads(FIXTURE.read_text(encoding="utf-8"))["vectors"]
except OSError, ValueError, KeyError:
    VECTORS = []


def test_corpus_is_readable():
    """A corpus that failed to parse must not read as a clean run."""
    assert FIXTURE.is_file(), f"no corpus at {FIXTURE}"
    assert len(VECTORS) >= _VENDORED_VECTOR_COUNT, (
        f"{FIXTURE} yielded {len(VECTORS)} vectors; the vendored corpus carries "
        f"{_VENDORED_VECTOR_COUNT}"
    )


def test_corpus_has_both_polarities():
    """Only-dangerous vectors cannot catch over-blocking, and vice versa."""
    assert any(v["readonly"] for v in VECTORS), "no read-only controls in corpus"
    assert any(not v["readonly"] for v in VECTORS), "no dangerous vectors in corpus"


@pytest.mark.parametrize("vector", VECTORS, ids=_ids(VECTORS))
def test_classify_matches_corpus(vector):
    classify = _load_classifier()
    verdict = classify(vector["command"])
    if vector["readonly"]:
        assert verdict is not None, (
            f"{vector['name']}: expected read-only, got a refusal — "
            f"over-blocking on {vector['command']!r}"
        )
    else:
        assert verdict is None, (
            f"{vector['name']}: auto-approved as {verdict!r}, but the corpus marks it "
            f"dangerous. {vector.get('why', '')}"
        )


@pytest.mark.skipif(
    not _DOTFILES_COPY.is_file(),
    reason=f"no dotfiles checkout at {_DOTFILES_COPY}, so there is nothing to diff against",
)
def test_the_vendored_corpus_matches_the_dotfiles_copy():
    """The vendored copy is a second copy by construction; this is what diffs it.

    Skips where the dotfiles checkout is absent (every CI run), since that is exactly where
    the vendored copy is the only one. Goes red on a deployed host the moment either side
    adds or edits a vector and the other does not follow.
    """
    assert json.loads(VENDORED.read_text(encoding="utf-8")) == json.loads(
        _DOTFILES_COPY.read_text(encoding="utf-8")
    ), f"{VENDORED} and {_DOTFILES_COPY} disagree; copy the edited side over the other"
