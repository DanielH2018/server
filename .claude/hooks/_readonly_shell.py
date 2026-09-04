#!/usr/bin/env python3
"""Shell-structure helpers for the auto-approve-readonly Bash classifier.

The part that reads an already-tokenized command line as shell: the substitution prefixes
and operator tokens the classifier refuses outright, the separators it splits statements and
pipeline stages on, and the redirect rules that decide whether a stage writes. The verdict
itself stays in `classify`, which calls these.

Imported by bare name from `auto-approve-readonly.py`; see `_hook_common.py` for why that
resolves both under the hook shim and under the tests. Stdlib-only.
"""

import re


_SUBST = ("`", "$(", "${")
_OP_TOKEN = re.compile(r"[();<>&|]+\Z")  # a token made ENTIRELY of shell operators
_SEQ = {";", "&&", "||"}  # sequential separators (each side a pipeline)
_FORBIDDEN = {"(", ")", "&"}  # subshell / backgrounding -- never read-only
_SAFE_REDIR_TARGETS = {"/dev/null"}  # the only write target we trust


def _split(tokens, seps):
    """Split a token list on any separator token in `seps`."""
    out, cur = [], []
    for t in tokens:
        if t in seps:
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    return out


def _is_redirect(tok):
    # a redirect operator carries a direction (< or >) and only redirect chars
    return bool(tok) and ("<" in tok or ">" in tok) and all(c in "<>&" for c in tok)


def _strip_redirects(stage):
    """Drop write-free redirects from a stage; return its argv, or None if unsafe.

    Allowed: input redirects (`< file` -- reading is read-only), writes/dups that
    target /dev/null (`>/dev/null`, `2>/dev/null`, `&>/dev/null`), and fd
    duplication (`2>&1`). Any redirect that writes a real file -> None.
    """
    argv = []
    i, n = 0, len(stage)
    while i < n:
        t = stage[i]
        if _is_redirect(t):
            if argv and argv[-1].isdigit():  # an attached fd number (e.g. 2 in 2>)
                argv.pop()
            if i + 1 >= n:
                return None
            target = stage[i + 1]
            if _OP_TOKEN.match(target):  # e.g. process substitution <( ... )
                return None
            if "<" in t and ">" not in t:  # pure input redirect: reading is OK
                pass
            elif ">&" in t or "<&" in t:  # fd duplication: target must be a fd
                if not target.isdigit():
                    return None
            else:  # >, >>, &> : writing
                if target not in _SAFE_REDIR_TARGETS:
                    return None
            i += 2
            continue
        argv.append(t)
        i += 1
    return argv
