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

SCOPE LIMIT, on purpose: the corpus lives in the chezmoi repo, which CI does not check out,
so these tests SKIP in CI and gate only on a machine that has the dotfiles. That is the
useful half — divergence is introduced by someone editing one classifier locally, and this
fails at that moment. Point COMMAND_VECTORS at another copy to run it elsewhere.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent

_DEFAULT_FIXTURE = (
    Path.home() / ".local/share/chezmoi/tests/fixtures/command-vectors.json"
)
FIXTURE = Path(os.environ.get("COMMAND_VECTORS", _DEFAULT_FIXTURE))


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
# A missing fixture yields an empty list, and test_corpus_is_readable is what reports that —
# an empty parametrize would otherwise pass as zero tests.
try:
    VECTORS = (
        json.loads(FIXTURE.read_text(encoding="utf-8"))["vectors"]
        if FIXTURE.is_file()
        else []
    )
except OSError, ValueError, KeyError:
    VECTORS = []


def test_corpus_is_readable():
    """A corpus that failed to parse must not read as a clean run."""
    if not FIXTURE.is_file():
        pytest.skip(f"shared corpus not present at {FIXTURE}")
    assert VECTORS, f"{FIXTURE} is present but yielded no vectors"


def test_corpus_has_both_polarities():
    """Only-dangerous vectors cannot catch over-blocking, and vice versa."""
    if not VECTORS:
        pytest.skip("shared corpus not present")
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
