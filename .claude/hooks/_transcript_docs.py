#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by inject-nested-docs.py to seed a context's state from its transcript
"""The `additionalContext` texts a hook gave one context, read back from its transcript.

`hook_contexts(transcript_path, agent_id, prefix)` returns every PreToolUse
`additionalContext` string that opens with `prefix` and still sits in the context window of
the session or subagent the payload names. `inject-nested-docs` calls it once per context,
when it holds no state file, so a context that inherited an injection is not given the doc
again. Two contexts start with injections they never received under their own state key:

- A fork subagent. Claude Code writes the fork's transcript under
  `<session>/subagents/agent-<agent_id>.jsonl`. A fork of the main session opens that file
  with a `fork-context-ref` record naming the parent transcript and the last parent record
  the fork inherited (`parentLastUuid`). A fork of a subagent copies the parent's records
  into its own file instead. Both forms are read.
- A resumed session. Resuming mints a new session id and copies the earlier transcript's
  records into the new one, so the new id has no state file and no `instructions.log` row.

A `compact_boundary` record discards everything before it, as the compaction discarded it
from the context.

Every failure reads as nothing found: a missing or unreadable transcript, a line that is not
JSON. The cost of a miss is one repeat injection.
"""

import json
import os
import re

_FORK_REF = "fork-context-ref"
_COMPACT = "compact_boundary"
_HOOK_CONTEXT = "hook_additional_context"


def _safe(ident):
    return re.sub(r"[^A-Za-z0-9_-]", "", ident or "")


def _record(line):
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _scan(path, prefix, stop_uuid=None, project_dir=None):
    """The texts opening with `prefix` in `path`, up to and including record `stop_uuid`.

    A `fork-context-ref` is followed only when `project_dir`, the directory holding the
    parent session's transcript, is given.
    """
    found = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return found
    with fh:
        for line in fh:
            # Parse only a line that can matter: a transcript runs to many MB of api blobs.
            if not (
                (_HOOK_CONTEXT in line and prefix in line)
                or _COMPACT in line
                or (project_dir and _FORK_REF in line)
                or (stop_uuid and stop_uuid in line)
            ):
                continue
            record = _record(line) or {}
            kind = record.get("type")
            attachment = record.get("attachment") or {}
            parent = _safe(record.get("parentSessionId"))
            if kind == _FORK_REF and project_dir and parent:
                parent_path = os.path.join(project_dir, f"{parent}.jsonl")
                found += _scan(parent_path, prefix, record.get("parentLastUuid"))
            elif kind == "system" and record.get("subtype") == _COMPACT:
                found.clear()
            elif kind == "attachment" and attachment.get("type") == _HOOK_CONTEXT:
                found += [
                    text
                    for text in attachment.get("content") or ()
                    if isinstance(text, str) and text.startswith(prefix)
                ]
            if stop_uuid and record.get("uuid") == stop_uuid:
                break
    return found


def hook_contexts(transcript_path, agent_id, prefix):
    """The `prefix` texts in the context window of the session, or of subagent `agent_id`.

    Args:
        transcript_path: the payload's `transcript_path`, the main session's transcript even
            in a subagent's payload. Empty reads as nothing found.
        agent_id: the payload's `agent_id`, or None for the main session.
        prefix: the tag the hook's texts open with, such as `[inject-nested-docs] `.

    Returns:
        Each matching `additionalContext` string, oldest first.
    """
    if not transcript_path:
        return []
    if not agent_id:
        return _scan(transcript_path, prefix)
    stem = transcript_path.removesuffix(".jsonl")
    own = os.path.join(stem, "subagents", f"agent-{_safe(agent_id)}.jsonl")
    return _scan(own, prefix, project_dir=os.path.dirname(transcript_path))
