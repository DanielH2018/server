#!/usr/bin/env python3
# gen-hooks: register
#   event: PreToolUse
#   matcher: Edit|Write|MultiEdit|NotebookEdit
#   timeout: 10
#   order: 60
#   args: --ask-on-cd
"""PreToolUse(Edit|Write) guard: deny direct edits to files you must not edit by hand.

Usage: Claude Code runs this hook on PreToolUse for the Edit, Write, MultiEdit and NotebookEdit
tools, passing `--ask-on-cd`. It reads the hook JSON payload on stdin. It prints a `deny` decision
JSON object or nothing, and it exits 0. `-h` or `--help` prints this text and exits 0 without
reading stdin.

Two classes, both always a mistake in this repo:

  1. A generated documentation page — rewritten by the docs-refresh cron, so a hand edit is
     lost at the next run. Scoped to the two generated trees and keyed off the
     `generated_from:` provenance banner a generator stamps.

  2. A SOPS-encrypted file (e.g. `secrets/secrets.yml`) — editing the ciphertext
     corrupts it, and pasting a plaintext secret in risks committing it. You must go
     through `sops`. (`.claude/rules/secrets.md`.) Detection is CONTENT-based, not
     path-based: `host_vars/*.yml` / `group_vars/all.yml` contain the substring
     "vars/" but are PLAINTEXT and must stay editable. It keys off the SOPS integrity
     MAC field — NOT a bare ciphertext substring, so a file that merely *mentions* the
     marker (a doc, a test, this hook) is not misclassified. The MAC is a `mac:` metadata
     line in the YAML and JSON forms, and a line opening with `sops_mac=` in the dotenv
     form, which SOPS writes for the cluster file `secrets/cluster-vars.sops.env`.

Reads the hook JSON on stdin. Emits a PreToolUse "deny" decision when the target is
one of the above; otherwise no output -> normal permission flow. The hook can only
ever BLOCK; it never approves.
"""

import os
import re
import sys
from pathlib import Path

from _hook_common import emit_pretooluse_decision, read_payload, repo_root


_SOPS_MAC_RE = re.compile(r'(?m)^\s*"?mac"?:\s*"?ENC\[AES256_GCM,')
# The dotenv store writes its metadata as plain `sops_<field>=<value>` lines (`SopsPrefix` in
# sops `stores/metadata.go`), so the MAC line of a SOPS dotenv file opens with `sops_mac=`.
_SOPS_DOTENV_MAC_RE = re.compile(r"(?m)^sops_mac=ENC\[AES256_GCM,")

# (tree, the generator that owns it). Rendered from the Ansible tree by
# scripts/docs/build_docs.py and rewritten by the docs-refresh cron, so a hand edit is lost at
# the next run. Listed rather than matched by a docs/ prefix: docs/ is mostly hand-written
# runbooks that must stay editable.
_GENERATED_DOC_TREES = (
    ("docs/reference", "the reference generators (scripts/docs/build_docs.py)"),
    (
        "docs/assets/generated",
        "scripts/infra_map/gen_infra_map.py (the map) or scripts/docs/gen_doc_fragments.py "
        "(fragments/)",
    ),
)


def is_sops_encrypted(text):
    """True iff text is a SOPS-encrypted document.

    Keys off the structural integrity MAC field in the sops metadata block (a `mac:` line, or a
    dotenv `sops_mac=` line, whose value is AES256-GCM ciphertext) rather than the bare marker
    substring — so a file that merely mentions the marker (docs, tests, this progress log) is
    not misclassified as ciphertext.
    """
    return bool(_SOPS_MAC_RE.search(text) or _SOPS_DOTENV_MAC_RE.search(text))


def _read_file(path, limit=1_048_576):
    """Read a file's text for marker detection.

    SOPS keeps its integrity MAC in the metadata block at the END of the file, so we read the whole
    thing (capped — a real secrets file is small; anything over the cap is not one). Missing/binary
    -> ''.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def classify(file_path, repo_root):
    """Return a deny reason if file_path must not be edited directly, else None.

    Relative paths resolve against repo_root (the session cwd). The generated-docs check
    matches a path segment and then reads the target for its provenance banner; the SOPS
    check reads the target's content.
    """
    if not file_path:
        return None
    target = (
        file_path if os.path.isabs(file_path) else os.path.join(repo_root, file_path)
    )
    target = os.path.normpath(target)

    # 1. generated documentation pages
    # Scoped to the two generated trees, never docs/ as a whole — docs/ is mostly
    # hand-written runbooks, and a guard that blocked those is a guard that gets
    # switched off.
    for tree, generator in _GENERATED_DOC_TREES:
        guard = os.path.normpath(os.path.join(repo_root, tree))
        if target == guard or target.startswith(guard + os.sep):
            rel = os.path.relpath(target, repo_root)
            # A docs/reference/ page is generator-owned when it carries the provenance
            # banner. Fails closed: only an existing file without the banner is exempt.
            if (
                target.endswith(".md")
                and os.path.isfile(target)
                and "generated_from:" not in _read_file(target)
            ):
                continue
            return (
                f"`{rel}` is generated by {generator} and is overwritten by the "
                f"docs-refresh cron. To change what appears here, change the generator "
                f"or the source it reads (see docs/adr/0001-mkdocs-site-with-generated-reference.md)."
            )

    # 2. SOPS-encrypted file (content-based)
    if is_sops_encrypted(_read_file(target)):
        rel = (
            os.path.relpath(target, repo_root)
            if target.startswith(repo_root + os.sep)
            else target
        )
        return (
            f"`{rel}` is SOPS-encrypted — don't edit it directly (you'd corrupt the "
            f"ciphertext or commit a plaintext secret). Edit it via `sops {rel}`, or use "
            f"the /add-secret skill. See .claude/rules/secrets.md."
        )
    return None


def find_repo_root(target, default):
    """Return the checkout that owns target as a str, or `default` when none does."""
    root = repo_root(Path(os.path.abspath(target)).parent)
    return str(root) if root else default


def main():
    """Read the hook payload from stdin and deny an Edit/Write to a protected file.

    Resolves the edited file's own repo root (so a worktree edit is checked against that
    worktree, not the primary checkout), then emits the PreToolUse deny decision `classify`
    returns. Always returns 0.
    """
    event = read_payload()
    call = event.edit() if event else None
    if call is None:
        return 0
    # Guard the checkout that owns the edited file, not the one this hook runs from: a
    # session edits files outside its own checkout by absolute path.
    hook_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    file_path = str(call.file_path)
    reason = classify(file_path, find_repo_root(file_path, hook_root))
    if reason:
        emit_pretooluse_decision("block-protected-edits", "deny", reason)
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
