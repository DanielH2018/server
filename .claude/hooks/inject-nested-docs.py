#!/usr/bin/env python3
# gen-hooks: library
#   reason: an arm of bash-pretool.py, which bash-pretool.sh runs through `uv run python`
"""PreToolUse(Bash): load the nested CLAUDE.md and `.claude/rules` a Bash read would skip.

WHY. Claude Code loads a role's `CLAUDE.md` (`nested_traversal`) and a `.claude/rules/*.md`
whose `paths:` glob matches (`path_glob_match`) only when the Read, Edit or Write tool touches
a matching path. Auto mode instructs the model to read files with `cat` / `sed -n` / `head`
through Bash instead, which fires neither. Measured over 176 transcripts on 2026-09-19: of 113
session×role pairs that read a role file through Bash and never used Read/Edit/Write on that
role, 74 (65%) never saw the role's CLAUDE.md; `.claude/rules/secrets.md` loaded 3 times
against 40 Bash commands naming `ansible/vars/secrets.yml`. `test_k8s_roles_have_claude_md.py`
guarantees every role has a doc; nothing guaranteed a session loads it. Issue #2125.

WHAT. Every token in the command that names a path on disk inside a git checkout selects
the docs the harness would have loaded for it: a `CLAUDE.md` in any ancestor directory below
the checkout root, and every `.claude/rules/*.md` whose `paths:` frontmatter matches. Each
doc is returned as PreToolUse `additionalContext` ONCE per session, and every injection is
logged to `instructions.log` with reason `bash_path_match`, so the same log that measured
the gap grades the fix. The root `CLAUDE.md` is never injected: it loads at session start.

ONLY DOCS THIS HOOK CHOSE ARE EVER READ. A path lifted from the command selects a directory;
the files opened are `CLAUDE.md` at an ancestor of that directory and the rule files under
`.claude/rules/`. Command text never names a file this hook reads.

THE PAYLOAD BUDGET IS A HARNESS PROPERTY. Read from the 2.1.267 bundle: on the local
command-hook path an `additionalContext` longer than 10,000 chars (`sgr=1e4`) is persisted to
disk and replaced by a preview stub (`Fme`), and on the remote-control wire path it is
truncated to 8,000 chars / 200 lines (`Uno`, `Hno`, `Cnn`). A 136 KB role doc therefore
cannot be inlined — the issue's "loads once" was written before that cap was measured. A doc
under `INLINE_MAX_CHARS` / `INLINE_MAX_LINES` is inlined; a larger one is injected as its
heading outline plus an instruction to read it. 111 of 141 nested docs fit inline on
2026-09-21.

Observability-shaped: never emits a decision, swallows every error and exits 0.
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from pathlib import PurePosixPath

from _hook_common import emit_pretooluse_context

# Under both harness caps with headroom for the wrapper text: local persist at 10,000 chars,
# remote truncation at 8,000 chars / 200 lines. Pinned by `test_inject_nested_docs.py`.
INLINE_MAX_CHARS = 7500
INLINE_MAX_LINES = 190

REASON = "bash_path_match"

# A session's set of injected docs is worthless once the session ends, and /tmp is shared
# between parallel sessions, so the file is keyed by session id (the nudge-land-sh shape).
_STATE_TTL_S = 24 * 3600

# Where a command's path tokens end. Quotes and backticks are stripped from the token after
# the split rather than treated as separators, so `'ansible/roles/k8s/foo/x.j2'` survives.
_SEPARATORS = re.compile(r"[\s;|&<>()`]+")
_QUOTES = "\"'"
_TRAILING_PUNCT = ",:."


def _load_logger():
    """The log-instructions.py module, loaded by path.

    The filename is hyphenated, so it is not importable by name — and one appender serves
    both the InstructionsLoaded event and this hook, so the log keeps one row format.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "log_instructions", os.path.join(here, "log-instructions.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_logger = _load_logger()


# ── which paths the command names ────────────────────────────────────────────────────


def path_tokens(command):
    """Every token of `command` that could be a path: carries a `/`, is not a flag."""
    found = []
    for raw in _SEPARATORS.split(command):
        token = raw.strip(_QUOTES).rstrip(_TRAILING_PUNCT)
        if "/" not in token or token.startswith("-") or "$" in token:
            continue
        found.append(token)
    return found


def _checkout_root(path):
    """The nearest ancestor of `path` holding a `.git` (a file in a worktree), or None."""
    current = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def named_paths(command, cwd):
    """(checkout_root, relative_path) for every existing path the command names.

    A token that does not exist on disk is retried as its directory, which is what a glob
    (`templates/*.j2`), a `file:line` and a not-yet-created file all resolve to. `s/a/b/`
    resolves to nothing either way.
    """
    seen = set()
    for token in path_tokens(command):
        candidate = token if os.path.isabs(token) else os.path.join(cwd, token)
        if not os.path.exists(candidate):
            candidate = os.path.dirname(candidate)
            if not os.path.exists(candidate):
                continue
        candidate = os.path.abspath(candidate)
        root = _checkout_root(candidate)
        if root is None or candidate == root:
            continue
        rel = os.path.relpath(candidate, root)
        if (root, rel) not in seen:
            seen.add((root, rel))
            yield root, rel


# ── which docs the harness would have loaded for them ────────────────────────────────


def rule_globs(rule_text):
    """The `paths:` globs in a rule file's YAML frontmatter, parsed without a YAML library.

    The block-list form is what this repo's three rule files use; the flow form is accepted
    so a fourth written that way still matches.
    """
    if not rule_text.startswith("---"):
        return []
    end = rule_text.find("\n---", 3)
    frontmatter = rule_text[3:end] if end != -1 else rule_text[3:]
    globs, in_paths = [], False
    for line in frontmatter.splitlines():
        flow = re.match(r"^paths:\s*\[(.*)\]\s*$", line)
        if flow:
            globs.extend(
                g.strip().strip(_QUOTES) for g in flow.group(1).split(",") if g.strip()
            )
            continue
        if re.match(r"^paths:\s*$", line):
            in_paths = True
            continue
        if in_paths:
            item = re.match(r"^\s+-\s*(.+?)\s*$", line)
            if item:
                globs.append(item.group(1).strip(_QUOTES))
                continue
            if line.strip():
                in_paths = False
    return globs


def docs_for(root, rel):
    """Repo-relative docs the harness loads for `rel` under `root`, in a stable order.

    A `CLAUDE.md` in every ancestor directory strictly below the root (the root's own is a
    session-start load), then every `.claude/rules/*.md` whose `paths:` glob matches.
    """
    docs = []
    directory = rel if os.path.isdir(os.path.join(root, rel)) else os.path.dirname(rel)
    while directory and directory != ".":
        doc = os.path.join(directory, "CLAUDE.md")
        if os.path.isfile(os.path.join(root, doc)):
            docs.append(doc)
        directory = os.path.dirname(directory)
    docs.reverse()
    rules_dir = os.path.join(root, ".claude", "rules")
    if os.path.isdir(rules_dir):
        target = PurePosixPath(rel.replace(os.sep, "/"))
        for name in sorted(os.listdir(rules_dir)):
            if not name.endswith(".md"):
                continue
            try:
                with open(os.path.join(rules_dir, name), encoding="utf-8") as fh:
                    globs = rule_globs(fh.read())
            except OSError:
                continue
            if any(target.full_match(g) for g in globs):
                docs.append(os.path.join(".claude", "rules", name))
    return docs


# ── once per session ─────────────────────────────────────────────────────────────────


def _state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "unknown"
    return os.path.join(tempfile.gettempdir(), f"claude-nested-docs-{safe}")


def injected_this_session(session_id, now=None):
    """The docs already injected for this session; a file older than the TTL is ignored."""
    now = time.time() if now is None else now
    try:
        with open(_state_path(session_id), encoding="utf-8") as fh:
            stamp, *paths = fh.read().splitlines()
        if now - float(stamp) < _STATE_TTL_S:
            return set(paths)
    except OSError, ValueError:
        pass
    return set()


def remember(session_id, docs, now=None):
    now = time.time() if now is None else now
    try:
        with open(_state_path(session_id), "w", encoding="utf-8") as fh:
            fh.write("\n".join([str(now), *sorted(docs)]))
    except OSError:
        # An unwritable temp dir must not break the session; the cost is a repeat injection.
        pass


def loaded_by_harness(session_id, doc, log_path=None):
    """True if `instructions.log` already holds a row for this session naming `doc`.

    The harness loads a doc itself when Read/Edit/Write touched the role earlier in the
    session; re-injecting it then is pure waste. The log and its one rotated backup are
    bounded at 256 KB each, and this runs at most once per doc per session.
    """
    sid = (session_id or "")[:8]
    if not sid:
        return False
    marker = f"[{sid:8}]"
    log = log_path or _logger.LOG
    for path in (log, log + ".1"):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if marker in line and doc in line.split():
                        return True
        except OSError:
            continue
    return False


# ── the payload ──────────────────────────────────────────────────────────────────────


def _outline(text):
    return [line.rstrip() for line in text.splitlines() if line.startswith("#")]


def render(root, doc, trigger, budget_chars):
    """One doc's block: inline when it fits the budget, an outline plus a read pointer otherwise."""
    with open(os.path.join(root, doc), encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.count("\n") + 1
    header = f"===== {doc} (applies to `{trigger}`) ====="
    if len(text) <= budget_chars and lines <= INLINE_MAX_LINES:
        return f"{header}\n{text.rstrip()}\n"
    outline = "\n".join(f"  {h}" for h in _outline(text)[:40])
    return (
        f"{header}\n"
        f"This file is {len(text):,} chars, over the {INLINE_MAX_CHARS:,}-char hook budget, "
        f"so only its outline is here. Read `{doc}` before you change anything it covers.\n"
        f"{outline}\n"
    )


def build_context(command, cwd, session_id, log_path=None):
    """(context_text, [(doc, trigger)]) for this command, or ("", []) when nothing is new."""
    already = injected_this_session(session_id)
    blocks, chosen = [], []
    remaining = INLINE_MAX_CHARS
    for root, rel in named_paths(command, cwd):
        for doc in docs_for(root, rel):
            if doc in already or any(doc == d for d, _ in chosen):
                continue
            if loaded_by_harness(session_id, doc, log_path):
                already.add(doc)
                continue
            try:
                block = render(root, doc, rel, remaining)
            except OSError:
                continue
            remaining = max(0, remaining - len(block))
            blocks.append(block)
            chosen.append((doc, rel))
    if not chosen:
        if already:
            remember(session_id, already)
        return "", []
    remember(session_id, already | {d for d, _ in chosen})
    preamble = (
        "[inject-nested-docs] Project instructions for paths this command names. Claude Code "
        "loads them for Read/Edit/Write but not for a Bash read, so this hook supplies each "
        "once per session.\n\n"
    )
    return preamble + "\n".join(blocks), chosen


def context(payload):
    """The docs `build_context` chooses for this payload, as injectable text, or None.

    Records each chosen doc in `instructions.log` as it goes, which is the side effect that
    keeps the injection once-per-session. The arm entry point `bash-pretool.py` calls;
    `main()` below is the same arm run as its own process.
    """
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "/" not in command:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or ""
    text, chosen = build_context(command, cwd, session_id)
    if not chosen:
        return None
    for doc, trigger in chosen:
        _logger.append_row(REASON, "Project", doc, session_id, " trigger=" + trigger)
    return text


def main():
    """Read the hook payload from stdin and inject the docs `context` chooses."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    text = context(data)
    if text:
        emit_pretooluse_context(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
