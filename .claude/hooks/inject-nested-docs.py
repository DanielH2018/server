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
doc is returned as PreToolUse `additionalContext` ONCE per session, and once more per
subagent (its payload's `agent_id`), whose context starts empty. Every injection is logged
to `instructions.log` with reason `bash_path_match`, so the same log that measured the gap
grades the fix. The root `CLAUDE.md` is never injected: it loads at session start.

RE-MEASURED 2026-09-26 (#2192) over the 5 days after the merge, same cross-tab both sides:
the no-doc share of Bash-only session×role pairs fell from 85% to 14% in main sessions,
but only from 82% to 56% in subagents, which the session-only key caused.

ONLY DOCS THIS HOOK CHOSE ARE EVER READ. A path lifted from the command selects a directory;
the files opened are `CLAUDE.md` at an ancestor of that directory and the rule files under
`.claude/rules/`. Command text never names a file this hook reads.

THE PAYLOAD BUDGET IS A HARNESS PROPERTY. Read from the 2.1.267 bundle: on the local
command-hook path an `additionalContext` longer than 10,000 chars (`sgr=1e4`) is persisted to
disk and replaced by a preview stub (`Fme`), and on the remote-control wire path it is
truncated to 8,000 chars / 200 lines (`Uno`, `Hno`, `Cnn`). A 136 KB role doc therefore
cannot be inlined — the issue's "loads once" was written before that cap was measured. A doc
under `INLINE_MAX_CHARS` / `INLINE_MAX_LINES` is inlined; a larger one is injected as its HEAD
up to the budget, then the headings of the sections the head cut off, then a read pointer.
111 of 141 nested docs fit inline on 2026-09-21.

THE HEAD, NOT THE HEADINGS. The over-budget form was the heading outline alone until #2650.
Sessions read the full doc after 61 of 262 outline injections over 2026-09-21..26 (23%),
against 69 of 425 Bash-only pairs (16%) before the hook existed, so the headings bought
almost nothing over no injection at all. The head spends the same budget on text a session
can act on without a second read: a role doc opens with its generated `## At a glance` block
and its operative rules.

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

# A head shorter than this teaches less than the headings it displaces, so a doc that cannot
# get this much text waits for a command with more budget left (the `render` deferral).
MIN_HEAD_CHARS = 1500

# The wrapper text, charged to both payload budgets before any doc is rendered.
_PREAMBLE = (
    "[inject-nested-docs] Project instructions for paths this command names. Claude Code "
    "loads them for Read/Edit/Write but not for a Bash read, so this hook supplies each "
    "once per session or subagent.\n\n"
)

# How many headings the trailer names. The whole outline of the busiest role docs fits.
TRAILER_MAX_HEADINGS = 40

REASON = "bash_path_match"

# Tags a log row written for a subagent; `loaded_by_harness` reads it back.
AGENT_FIELD = "agent="

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
    """Every token of `command` that could be a path: carries a `/`, is not a flag.

    A token naming a path inside a git object — `git show origin/master:ansible/roles/k8s/foo/
    tasks/main.yml` — yields the whole token AND the part after its last `:`, so the caller's
    existence check keeps whichever of the two is a real path. Both are emitted rather than
    one chosen here, because only the caller knows the cwd. A `file:line` token is unaffected:
    the part after its last `:` is a line number carrying no `/`, and the caller's dirname
    fallback already resolves it.
    """
    found = []
    for raw in _SEPARATORS.split(command):
        token = raw.strip(_QUOTES).rstrip(_TRAILING_PUNCT)
        if "/" not in token or token.startswith("-") or "$" in token:
            continue
        found.append(token)
        after_ref = token.rpartition(":")[2]
        if after_ref != token and "/" in after_ref:
            found.append(after_ref)
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
    resolves to nothing either way. `path_tokens` supplies the second form of a
    `git show <ref>:<path>` token, so whichever of the two exists is the one kept.
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


def context_key(session_id, agent_id=None):
    """The id that "once" is counted against: the session, or one subagent inside it.

    A subagent's payload carries its parent's `session_id` plus its own `agent_id`, and its
    context window starts empty. Keyed on the session alone, a doc the parent (or a sibling
    subagent) already received was never given to the subagent: 95 of the 122 no-doc
    session×role pairs measured over 2026-09-21..26 were subagents (#2192).
    """
    return f"{session_id}-agent-{agent_id}" if agent_id else session_id


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


def loaded_by_harness(session_id, doc, log_path=None, agent_id=None):
    """True if `instructions.log` already holds a row for this session naming `doc`.

    The harness loads a doc itself when Read/Edit/Write touched the role earlier in the
    session; re-injecting it then is pure waste. The log and its one rotated backup are
    bounded at 256 KB each, and this runs at most once per doc per session.

    A row carries only the 8-char session prefix, so the parent and every subagent share
    it. A row tagged `agent=` belongs to a subagent and never counts for the parent. A
    subagent skips the log entirely, because an untagged row may be its parent's; the
    cost is a repeat load when the subagent itself used Read on the role first.
    """
    sid = (session_id or "")[:8]
    if not sid or agent_id:
        return False
    marker = f"[{sid:8}]"
    log = log_path or _logger.LOG
    for path in (log, log + ".1"):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    fields = line.split()
                    if (
                        marker in line
                        and doc in fields
                        and not any(f.startswith(AGENT_FIELD) for f in fields)
                    ):
                        return True
        except OSError:
            continue
    return False


# ── the payload ──────────────────────────────────────────────────────────────────────


def _outline(lines):
    return [line.rstrip() for line in lines if line.startswith("#")]


def _head_lines(lines, max_chars, max_lines):
    """How many leading lines of `lines` fit the budget, cut at a heading where one is near.

    A cut mid-section hands the model half a rule, so the count rolls back to the last
    heading inside what fit. It rolls back only past the later half of those lines: a
    heading in the first half would throw away most of the text the budget paid for.
    """
    used = count = 0
    for line in lines:
        if count >= max_lines or used + len(line) + 1 > max_chars:
            break
        used += len(line) + 1
        count += 1
    for i in range(count - 1, 0, -1):
        if lines[i].startswith("#"):
            return i if i * 2 >= count else count
    return count


def _head_block(doc, text, header, budget_chars, budget_lines):
    """A doc over the budget as its head plus the headings its head cut off, or None.

    None means the budget left by earlier docs in this command buys less head than
    `MIN_HEAD_CHARS`; the caller defers the doc to the next command naming the path.
    """
    lines = text.splitlines()
    notice = (
        f"This file is {len(text):,} chars, over the {INLINE_MAX_CHARS:,}-char hook budget, "
        f"so this is its head. Read `{doc}` for the rest before you change anything it covers."
    )
    cut_line = "----- sections of this file NOT shown above -----"
    fixed = len(header) + len(notice) + len(cut_line) + 8
    # Two passes. The first reserves room for the doc's whole outline, since the cut is not
    # known yet; the second reserves only the headings that first cut left over, which frees
    # the rest for head text. The final trailer is a subset of the one the last pass reserved
    # for — a later cut can only drop headings — so the block stays under the budget.
    trailer, cut = _outline(lines)[:TRAILER_MAX_HEADINGS], 0
    for _ in range(2):
        head_chars = budget_chars - fixed - sum(len(h) + 3 for h in trailer)
        head_lines = budget_lines - len(trailer) - 4
        if head_chars < MIN_HEAD_CHARS or head_lines < 20:
            return None
        cut = _head_lines(lines, head_chars, head_lines)
        trailer = _outline(lines[cut:])[:TRAILER_MAX_HEADINGS]
    block = f"{header}\n{notice}\n" + "\n".join(lines[:cut]).rstrip() + "\n"
    if trailer:
        block += cut_line + "\n" + "\n".join(f"  {h}" for h in trailer) + "\n"
    return block


def render(root, doc, trigger, budget_chars, budget_lines):
    """One doc's block, or None when the doc should wait for a later command.

    A doc that fits the hook budget on its own is inlined, or returns None when earlier
    docs in this command have used up the budget. The caller leaves it unrecorded, so the
    next command naming the path inlines it. Outlining it instead would be final: 47 of
    262 outline injections over 2026-09-21..26 were docs that fit alone (#2192). A doc
    over the budget on its own is injected as its head — see `_head_block`.

    Both budgets are what is LEFT of the payload, not the per-doc cap, because the harness
    caps the whole `additionalContext`. The line budget only started to bind once the head
    form landed: a heading outline cost a handful of lines, while a head can spend nearly
    all 190 and push a second doc's block past the 200-line wire truncation (#2650).
    """
    with open(os.path.join(root, doc), encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.count("\n") + 1
    header = f"===== {doc} (applies to `{trigger}`) ====="
    if len(text) <= INLINE_MAX_CHARS and lines <= INLINE_MAX_LINES:
        block = f"{header}\n{text.rstrip()}\n"
        if len(block) > budget_chars or block.count("\n") > budget_lines:
            return None
        return block
    return _head_block(doc, text, header, budget_chars, budget_lines)


def build_context(command, cwd, session_id, log_path=None, agent_id=None):
    """(context_text, [(doc, trigger)]) for this command, or ("", []) when nothing is new."""
    key = context_key(session_id, agent_id)
    already = injected_this_session(key)
    blocks, chosen = [], []
    remaining = INLINE_MAX_CHARS - len(_PREAMBLE)
    remaining_lines = INLINE_MAX_LINES - _PREAMBLE.count("\n")
    for root, rel in named_paths(command, cwd):
        for doc in docs_for(root, rel):
            if doc in already or any(doc == d for d, _ in chosen):
                continue
            if loaded_by_harness(session_id, doc, log_path, agent_id):
                already.add(doc)
                continue
            try:
                block = render(root, doc, rel, remaining, remaining_lines)
            except OSError:
                continue
            if block is None:
                continue
            # The +1 is the blank line `"\n".join` puts between two blocks.
            remaining = max(0, remaining - len(block) - 1)
            remaining_lines = max(0, remaining_lines - block.count("\n") - 1)
            blocks.append(block)
            chosen.append((doc, rel))
    if not chosen:
        if already:
            remember(key, already)
        return "", []
    remember(key, already | {d for d, _ in chosen})
    return _PREAMBLE + "\n".join(blocks), chosen


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
    agent_id = payload.get("agent_id") or None
    text, chosen = build_context(command, cwd, session_id, agent_id=agent_id)
    if not chosen:
        return None
    tag = f" {AGENT_FIELD}{agent_id}" if agent_id else ""
    for doc, trigger in chosen:
        _logger.append_row(
            REASON, "Project", doc, session_id, " trigger=" + trigger + tag
        )
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
