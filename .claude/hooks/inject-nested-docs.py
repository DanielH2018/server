#!/usr/bin/env python3
# gen-hooks: library
#   reason: an arm of bash-pretool.py, which run-hook.sh runs through `uv run python`
"""PreToolUse(Bash): load the nested CLAUDE.md and `.claude/rules` a Bash read would skip.

Usage: `bash-pretool.py` loads this file as an arm on PreToolUse:Bash, and it also runs
standalone. As a hook it reads the JSON payload on stdin, prints an `additionalContext` JSON
object or nothing, and always exits 0. `-h` or `--help` prints this text and exits 0 without
reading stdin. `--report [--root DIR]` reads no stdin and writes nothing. It prints the budgets,
then every non-root `CLAUDE.md` and `.claude/rules/*.md` the checkout tracks with its margins,
tightest first, then the root `CLAUDE.md`. It exits non-zero on any error.

WHY. Claude Code loads a role's `CLAUDE.md` (`nested_traversal`) and a `.claude/rules/*.md`
whose `paths:` glob matches (`path_glob_match`) only when the Read, Edit or Write tool touches
a matching path. Auto mode instructs the model to read files with `cat` / `sed -n` / `head`
through Bash instead, which fires neither. `tests/repo/docs/test_role_claude_md.py` guarantees
every role has a doc; nothing guaranteed a session loads it.

WHAT. Every token in the command that names a path on disk inside a git checkout selects
the docs the harness would have loaded for it: a `CLAUDE.md` in any ancestor directory below
the checkout root, and every `.claude/rules/*.md` whose `paths:` frontmatter matches. Each
doc is returned as PreToolUse `additionalContext` ONCE per session, and once more per
subagent (its payload's `agent_id`). A context the hook holds no state for (a fork, a resumed
session, a session after a reboot) is first credited with every block its transcript shows
it already holds (`inherited_state`). Every injection is logged
to `instructions.log` with reason `bash_path_match`, so the same log that measured the gap
grades the fix. Each row ends in `bytes=<n>`, the UTF-8 size of the block injected for its
doc, and the row of a doc delivered as a head, an outline or a pointer carries `form=head`,
`form=outline` or `form=pointer` before that. The root
`CLAUDE.md` is never injected: it loads at session start.

ONLY DOCS THIS HOOK CHOSE ARE EVER READ. A path lifted from the command selects a directory;
the files opened are `CLAUDE.md` at an ancestor of that directory and the rule files under
`.claude/rules/`. Command text never names a file this hook reads. The one other file read is
the context's transcript, which the payload's `transcript_path` and `agent_id` name.

THE PAYLOAD BUDGET IS A HARNESS PROPERTY. On the local command-hook path an
`additionalContext` longer than 10,000 chars (`sgr=1e4`) is persisted to disk and replaced by
a preview stub (`Fme`), and on the remote-control wire path it is truncated to 8,000 chars /
200 lines (`Uno`, `Hno`, `Cnn`). A 136 KB role doc therefore cannot be inlined. A doc under
`INLINE_MAX_CHARS` / `INLINE_MAX_LINES` is inlined; a larger one is injected as its HEAD up to
the budget, then the headings of the sections the head cut off, then a read pointer.

EDITING RULES ARRIVE WITH A WRITE. A rule outside `READ_RULES` is injected only for a path the
command writes (`block-protected-bash.written_paths`), or for every named path under an inline
interpreter. Rules are mostly injected on reads, but 23% of rule-scoped writes go through
Bash, so they are not dropped from Bash outright. The measurements are in
`docs/claude-tooling.md`. A rule in `POINTER_RULES` gets one pointer line on a read, logged
`form=pointer`, and still arrives whole with the first write.

EACH SESSION HAS A BYTE BUDGET. Every byte the hook injects is charged to
`SESSION_BUDGET_BYTES`: the preamble, the separators and every block. Role `CLAUDE.md` files
and editing rules are inlined, whole or as a head, until their blocks would take the session
past it. A doc after that arrives as its outline: the headings and a read pointer, logged
`form=outline`. The rules in `ALWAYS_WHOLE` are charged but never outlined, because a
read-only grep is where the secrets rule matters. The full preamble goes with a session's
first injection, and every later one carries `_SHORT_PREAMBLE`.

THE HEAD, NOT THE HEADINGS. The over-budget form was the heading outline alone until #2650.
Headings alone bought almost nothing over no injection: sessions read the full doc after only
23% of outline injections, against 16% of Bash-only pairs with no hook. The head spends the
same budget on text a session can act on without a second read: a role doc opens with its
generated `## At a glance` block and its operative rules. No CI check holds a doc under the
budget, so the head path is permanent: `--report` names each doc that reads as a head, and
`fact_status.py report` ranks those docs by the `form=head` rows they logged. A doc's
budget is what `_fits_inline` leaves it, `INLINE_MAX_CHARS` less the preamble and the header,
not `INLINE_MAX_CHARS` itself (#3245), and the head's notice states that budget.

Observability-shaped: the hook path never emits a decision, swallows every error and exits 0.
`--report` is dispatched before that swallow, so a report that fails says so.
"""

import importlib.util
import os
import re
import sys
import time
from pathlib import PurePosixPath
from typing import NamedTuple

from _hook_common import (
    BashCall,
    append_instructions_row,
    emit_pretooluse_context,
    instructions_log_path,
    read_payload,
    session_state_path,
    tagged,
)
from _inline_budget import blocks, inline_margins, print_report, rule_globs
from _inline_budget import header as _header
from _transcript_docs import hook_contexts

# Under both harness caps: local persist 10,000 chars, remote truncation 8,000 chars / 200 lines.
INLINE_MAX_CHARS = 7500
INLINE_MAX_LINES = 190

# A head shorter than this teaches less than the headings it displaces, so the doc waits for a
# command with more budget.
MIN_HEAD_CHARS = 1500

# The wrapper text, charged to both payload budgets before any doc is rendered.
_PREAMBLE = tagged(
    "inject-nested-docs",
    "Project instructions for paths this command names. Claude Code "
    "loads them for Read/Edit/Write but not for Bash, so this hook supplies each once per "
    "session or subagent (an editing rule on the first write).\n\n",
)
_SHORT_PREAMBLE = tagged(
    "inject-nested-docs",
    "More project instructions for paths this command names.\n\n",
)

# Everything a session is injected before its role docs and editing rules arrive as outlines.
SESSION_BUDGET_BYTES = 8000

# How many headings the trailer names. The whole outline of the busiest role docs fits.
TRAILER_MAX_HEADINGS = 40

REASON = "bash_path_match"

# The rules a READ needs whole: `secrets.md` warns before a content grep prints a credential.
READ_RULES = frozenset({"secrets.md"})

# Editing rules a read gets as one pointer line, so the first write still inlines them whole.
POINTER_RULES = frozenset({"generated-docs.md"})

# Inlined in full on every first match however much of the session budget is spent.
ALWAYS_WHOLE = frozenset({"secrets.md", "generated-docs.md"})

# The state-file entry recording that a doc's pointer, not the doc, was sent.
_POINTER_KEY = "pointer:"

# An inline interpreter writes through code the command cannot show.
_INLINE_INTERPRETER = re.compile(
    r"\b(?:python3?|perl|ruby|node)\s+(?:-\s|-c\b|-e\b|-$)", re.M
)

# Tags a log row written for a subagent; `loaded_by_harness` reads it back.
AGENT_FIELD = "agent="

# A session's set of injected docs is worthless once the session ends, and /tmp is shared
# between parallel sessions, so the file is keyed by session id.
_STATE_TTL_S = 24 * 3600

# Quotes and backticks are stripped after the split, so a quoted path survives.
_SEPARATORS = re.compile(r"[\s;|&<>()`]+")
_QUOTES = "\"'"
_TRAILING_PUNCT = ",:."


def _load_written_paths():
    """`written_paths` from block-protected-bash.py, loaded by path: one detector, two hooks."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "block_protected_bash", os.path.join(here, "block-protected-bash.py")
    )
    assert spec and spec.loader, "spec_from_file_location found no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.written_paths


written_paths = _load_written_paths()


# ── which paths the command names ────────────────────────────────────────────────────


def path_tokens(command):
    """Every token of `command` that could be a path: carries a `/`, is not a flag.

    A token naming a path inside a git object — `git show origin/master:k8s/roles/foo/
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


def written_targets(command, cwd):
    """Written (root, rel) pairs as `named_paths` resolves them; None under an interpreter."""
    if _INLINE_INTERPRETER.search(command):
        return None
    return set(named_paths(" ".join(written_paths(command)), cwd))


def _rule_name(doc):
    """The file name of a `.claude/rules` doc, or None for a `CLAUDE.md`."""
    head, name = os.path.split(doc)
    return name if head == os.path.join(".claude", "rules") else None


def _is_edit_rule(doc):
    """True for a `.claude/rules` file that governs editing rather than reading."""
    name = _rule_name(doc)
    return name is not None and name not in READ_RULES


def _is_always_whole(doc):
    return _rule_name(doc) in ALWAYS_WHOLE


def _is_pointer_rule(doc):
    return _rule_name(doc) in POINTER_RULES


# ── which docs the harness would have loaded for them ────────────────────────────────


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
    context window starts empty unless it is a fork. Keyed on the session alone, a doc the
    parent (or a sibling subagent) already received would never be given to the subagent.
    A fork's inherited docs come from its transcript instead (`inherited_state`).
    """
    return f"{session_id}-agent-{agent_id}" if agent_id else session_id


def _state_path(session_id):
    return session_state_path("nested-docs", session_id)


class SessionState(NamedTuple):
    """What this hook has already given one session or subagent.

    Attributes:
        docs: the docs injected, or loaded by the harness, so never injected again.
        spent: UTF-8 bytes of the blocks charged to `SESSION_BUDGET_BYTES`.
        preambled: whether the full `_PREAMBLE` has been sent.
    """

    docs: set
    spent: int = 0
    preambled: bool = False


def injected_this_session(session_id, now=None):
    """This session's `SessionState`, or None when no state file younger than the TTL exists.

    The first line is `<stamp> <spent> <preambled>`. A file holding only the stamp predates
    the budget, and reads as nothing spent.
    """
    now = time.time() if now is None else now
    try:
        with open(_state_path(session_id), encoding="utf-8") as fh:
            first, *paths = fh.read().splitlines()
        stamp, *rest = first.split()
        if now - float(stamp) < _STATE_TTL_S:
            spent = int(rest[0]) if rest else 0
            preambled = rest[1:] == ["1"] if rest else bool(paths)
            return SessionState(set(paths), spent, preambled)
    except OSError, ValueError:
        pass
    return None


def inherited_state(transcript_path, agent_id=None):
    """The `SessionState` that the context's transcript shows this hook already gave it.

    A fork inherits its parent's injections, and a resumed session its earlier id's, under a
    state key that has no file yet; a reboot also empties the temp dir that holds the file.
    Every block found is charged to the budget, and a pointer block records its
    `_POINTER_KEY` entry, so the doc itself still arrives with the first write.
    """
    texts = hook_contexts(transcript_path, agent_id, tagged("inject-nested-docs", ""))
    docs = {
        (_POINTER_KEY if _form(block) == _POINTER_FORM else "") + doc
        for text in texts
        for doc, block in blocks(text)
    }
    spent = sum(len(text.encode("utf-8")) for text in texts)
    return SessionState(docs, spent, bool(texts))


def remember(session_id, state, now=None):
    now = time.time() if now is None else now
    first = f"{now} {state.spent} {int(state.preambled)}"
    try:
        with open(_state_path(session_id), "w", encoding="utf-8") as fh:
            fh.write("\n".join([first, *sorted(state.docs)]))
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
    cost is a repeat load when the subagent itself used Read on the role first. A
    `form=pointer` row never counts, because it delivered a pointer, not the doc. A resumed
    session's earlier rows carry its earlier id, so they never count either.
    """
    sid = (session_id or "")[:8]
    if not sid or agent_id:
        return False
    marker = f"[{sid:8}]"
    log = log_path or instructions_log_path()
    for path in (log, log + ".1"):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    fields = line.split()
                    if (
                        marker in line
                        and doc in fields
                        and not any(f.startswith(AGENT_FIELD) for f in fields)
                        and f"form={_POINTER_FORM}" not in fields
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


_CUT_LINE = "----- sections of this file NOT shown above -----"
_HEAD_NOTICE_MARK = "so this is its head."

# A head shorter than this many lines is deferred, like one under `MIN_HEAD_CHARS`.
_MIN_HEAD_LINES = 20


def _notice(doc, text, header):
    """The head's first line: the doc's size against the budget `_fits_inline` leaves it.

    That budget is the payload less the preamble and this doc's header, not `INLINE_MAX_CHARS`,
    and the line names only the limit the doc is over.
    """
    body = text.rstrip()
    chars, lines = len(body), body.count("\n") + 1
    chars_left, lines_left = _margins(text, header)
    over = []
    if chars_left < 0 or lines_left >= 0:
        over.append((f"{chars:,} chars", f"{chars + chars_left:,}-char"))
    if lines_left < 0:
        over.append((f"{lines:,} lines", f"{lines + lines_left:,}-line"))
    size, budget = (" and ".join(part) for part in zip(*over, strict=True))
    return (
        f"This file is {size}, over the {budget} budget this hook leaves it, "
        f"{_HEAD_NOTICE_MARK} Read `{doc}` for the rest before you change anything it covers."
    )


_OUTLINE_NOTICE_MARK = "so this is its outline."
_POINTER_NOTICE_MARK = "so this is its pointer."
_POINTER_FORM = "pointer"


def _form(block):
    """`head`, `outline`, `pointer` or None for a whole doc, read from the block's notice line.

    Only the second line counts: a doc delivered whole may quote a notice in its body.
    """
    notice = (block.split("\n", 2) + [""])[1]
    if _HEAD_NOTICE_MARK in notice:
        return "head"
    if _OUTLINE_NOTICE_MARK in notice:
        return "outline"
    if _POINTER_NOTICE_MARK in notice:
        return _POINTER_FORM
    return None


def _pointer_block(doc, header):
    """An editing rule on a read, as one line naming it."""
    notice = (
        f"This rule governs writes and this command only reads, {_POINTER_NOTICE_MARK} "
        f"Read `{doc}` before you change this path; a Bash write inlines it whole."
    )
    return f"{header}\n{notice}\n"


def _outline_block(doc, text, header):
    """A doc the session budget cannot pay for, as its headings and a read pointer."""
    notice = (
        f"This session has spent the {SESSION_BUDGET_BYTES:,} bytes this hook inlines, "
        f"{_OUTLINE_NOTICE_MARK} Read `{doc}` before you change anything it covers."
    )
    headings = _outline(text.splitlines())[:TRAILER_MAX_HEADINGS]
    return f"{header}\n{notice}\n" + "".join(f"  {h}\n" for h in headings)


def _head_overhead(doc, text, header, trailer):
    """(chars, lines) a head block spends on everything but its head text."""
    fixed = len(header) + len(_notice(doc, text, header)) + len(_CUT_LINE) + 8
    return fixed + sum(len(h) + 3 for h in trailer), len(trailer) + 4


def _head_block(doc, text, header, budget_chars, budget_lines):
    """A doc over the budget as its head plus the headings its head cut off, or None.

    None means the budget left by earlier docs in this command buys less head than
    `MIN_HEAD_CHARS`; the caller defers the doc to the next command naming the path.
    """
    lines = text.splitlines()
    # Two passes: the first reserves the whole outline, the second only the headings the first cut
    # left.
    trailer, cut = _outline(lines)[:TRAILER_MAX_HEADINGS], 0
    for _ in range(2):
        over_chars, over_lines = _head_overhead(doc, text, header, trailer)
        head_chars = budget_chars - over_chars
        head_lines = budget_lines - over_lines
        if head_chars < MIN_HEAD_CHARS or head_lines < _MIN_HEAD_LINES:
            return None
        cut = _head_lines(lines, head_chars, head_lines)
        trailer = _outline(lines[cut:])[:TRAILER_MAX_HEADINGS]
    notice = _notice(doc, text, header)
    block = f"{header}\n{notice}\n" + "\n".join(lines[:cut]).rstrip() + "\n"
    if trailer:
        block += _CUT_LINE + "\n" + "\n".join(f"  {h}" for h in trailer) + "\n"
    return block


def _read(root, doc):
    with open(os.path.join(root, doc), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _margins(text, header):
    """The (chars, newlines) `text`'s whole-doc block leaves of the payload after the preamble."""
    return inline_margins(
        text,
        header,
        INLINE_MAX_CHARS - len(_PREAMBLE),
        INLINE_MAX_LINES - _PREAMBLE.count("\n"),
    )


def _fits_inline(text, header):
    """True when `render`'s whole-doc block fits the payload after the preamble.

    A doc between that and `INLINE_MAX_CHARS` fits no command and is deferred forever.
    """
    chars_left, lines_left = _margins(text, header)
    return chars_left >= 0 and lines_left >= 0


def head_floor(root, doc, trigger):
    """The smallest (chars, lines) budget `render` injects `doc` as a head for.

    Returns None for a doc that `render` inlines whole instead. The first pass of
    `_head_block` is the binding one, since it reserves the doc's whole outline.
    """
    text = _read(root, doc)
    header = _header(doc, trigger)
    if _fits_inline(text, header):
        return None
    trailer = _outline(text.splitlines())[:TRAILER_MAX_HEADINGS]
    chars, lines = _head_overhead(doc, text, header, trailer)
    return chars + MIN_HEAD_CHARS, lines + _MIN_HEAD_LINES


def render(root, doc, trigger, budget_chars, budget_lines):
    """One doc's block, or None when the doc should wait for a later command.

    A doc that fits the hook budget on its own is inlined, or returns None when earlier
    docs in this command have used up the budget. The caller leaves it unrecorded, so the
    next command naming the path inlines it. Outlining it instead would be final, so a doc
    that fits alone would never be inlined. A doc over the budget on its own is injected
    as its head — see `_head_block`.

    Both budgets are what is LEFT of the payload, not the per-doc cap, because the harness
    caps the whole `additionalContext`. The line budget binds for the head form: a head
    can spend nearly all 190 lines and push a second doc's block past the 200-line wire
    truncation. Which doc gets the budget first is `build_context`'s call.
    """
    text = _read(root, doc)
    header = _header(doc, trigger)
    if _fits_inline(text, header):
        block = f"{header}\n{text.rstrip()}\n"
        if len(block) > budget_chars or block.count("\n") > budget_lines:
            return None
        return block
    return _head_block(doc, text, header, budget_chars, budget_lines)


def build_context(
    command, cwd, session_id, log_path=None, agent_id=None, transcript_path=""
):
    """(context_text, [(doc, trigger, block)]) for this command, or ("", []) when nothing is new.

    Docs that fit whole are rendered before any doc injected as its head, in the order
    `named_paths` and `docs_for` give. A head that spent the payload first would defer a
    short `.claude/rules` file matched by the same command. The whole docs cannot starve
    the head in turn, because they only get the budget left after the first head's
    `head_floor`. A whole doc that does not fit under that reserve is deferred instead.
    `python-layout.md` is 6,960 chars, so it always defers beside a head. An `ALWAYS_WHOLE`
    rule goes before the other whole docs, and a pointer before both.

    Every byte of the returned text is charged to `SESSION_BUDGET_BYTES`. A block outside
    `ALWAYS_WHOLE` that would take the session past it is replaced by the doc's
    `_outline_block`, which is recorded and never re-sent. A `POINTER_RULES` rule on a read
    is recorded under `_POINTER_KEY`, so its doc stays unsent for the first write.
    """
    key = context_key(session_id, agent_id)
    state = injected_this_session(key)
    if state is None:
        # Written even when empty, so the transcript is read once per context, not per call.
        state = inherited_state(transcript_path, agent_id)
        remember(key, state)
    already = state.docs
    written = written_targets(command, cwd)
    # doc -> (root, rel, floor, pointer); a write of the doc replaces a pointer to it.
    candidates = {}
    for root, rel in named_paths(command, cwd):
        writes = written is None or (root, rel) in written
        for doc in docs_for(root, rel):
            if doc in already or (doc in candidates and not candidates[doc][3]):
                continue
            pointer = not writes and _is_edit_rule(doc)
            if pointer and (
                not _is_pointer_rule(doc)
                or doc in candidates
                or _POINTER_KEY + doc in already
            ):
                continue
            if loaded_by_harness(session_id, doc, log_path, agent_id):
                already.add(doc)
                candidates.pop(doc, None)
                continue
            try:
                floor = None if pointer else head_floor(root, doc, rel)
            except OSError:
                continue
            candidates[doc] = (root, rel, floor, pointer)
    # Only the first head is reserved for; a second head in the same command defers.
    floors = [c[2] for c in candidates.values() if c[2] is not None]
    reserve, reserve_lines = (floors[0][0] + 1, floors[0][1] + 1) if floors else (0, 0)
    preamble = _SHORT_PREAMBLE if state.preambled else _PREAMBLE
    spent = state.spent + len(preamble.encode("utf-8"))
    blocks, chosen = [], []
    remaining = INLINE_MAX_CHARS - len(_PREAMBLE)
    remaining_lines = INLINE_MAX_LINES - _PREAMBLE.count("\n")
    order = sorted(
        candidates.items(),
        key=lambda c: (c[1][2] is not None, not c[1][3], not _is_always_whole(c[0])),
    )
    for doc, (root, rel, floor, pointer) in order:
        whole = floor is None
        # The 1 is the newline `"\n".join` puts before every block after the first.
        separator = 1 if blocks else 0
        try:
            if pointer:
                block = _pointer_block(doc, _header(doc, rel))
            else:
                block = render(
                    root,
                    doc,
                    rel,
                    remaining - reserve if whole else remaining,
                    remaining_lines - reserve_lines if whole else remaining_lines,
                )
            if block is None:
                continue
            size = separator + len(block.encode("utf-8"))
            if spent + size > SESSION_BUDGET_BYTES and not _is_always_whole(doc):
                block = _outline_block(doc, _read(root, doc), _header(doc, rel))
                if len(block) > remaining or block.count("\n") > remaining_lines:
                    continue
                size = separator + len(block.encode("utf-8"))
        except OSError:
            continue
        spent += size
        remaining = max(0, remaining - len(block) - 1)
        remaining_lines = max(0, remaining_lines - block.count("\n") - 1)
        blocks.append(block)
        chosen.append((doc, rel, block))
    if not chosen:
        if already:
            remember(key, state._replace(docs=already))
        return "", []
    sent = {
        (_POINTER_KEY if _form(b) == _POINTER_FORM else "") + d for d, _, b in chosen
    }
    remember(key, SessionState(already | sent, spent, True))
    return preamble + "\n".join(blocks), chosen


def context(call: BashCall):
    """The docs `build_context` chooses for this call, as injectable text, or None.

    Records each chosen doc in `instructions.log` as it goes, which is the side effect that
    keeps the injection once-per-session. The arm entry point `bash-pretool.py` calls;
    `main()` below is the same arm run as its own process.
    """
    if "/" not in call.command:
        return None
    session_id = call.session_id
    agent_id = call.agent_id
    text, chosen = build_context(
        call.command,
        str(call.cwd),
        session_id,
        agent_id=agent_id,
        transcript_path=call.transcript_path,
    )
    if not chosen:
        return None
    tag = f" {AGENT_FIELD}{agent_id}" if agent_id else ""
    for doc, trigger, block in chosen:
        # The block, not the file: `fact_status.py report` totals what entered context.
        size = f" bytes={len(block.encode('utf-8'))}"
        form = f" form={_form(block)}" if _form(block) else ""
        append_instructions_row(
            REASON,
            "Project",
            doc,
            session_id,
            " trigger=" + trigger + tag + form + size,
        )
    return text


def main():
    """Read the hook payload from stdin and inject the docs `context` chooses."""
    event = read_payload()
    call = event.bash() if event else None
    if call is None:
        return 0
    text = context(call)
    if text:
        emit_pretooluse_context(text)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if any(arg in ("-h", "--help") for arg in args):
        print(__doc__.strip())
        sys.exit(0)
    # Before the try: the swallow below would turn a failed report into an empty exit 0.
    if "--report" in args:
        if "--root" in args:
            root = args[args.index("--root") + 1]
        else:
            root = _checkout_root(os.path.dirname(os.path.abspath(__file__)))
        print_report(root, INLINE_MAX_CHARS, INLINE_MAX_LINES, _PREAMBLE)
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
