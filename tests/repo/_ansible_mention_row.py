"""The census row holding live docs free of Ansible as a working part of the repo."""

import re

from _row_table import Census, Subject, tracked
from lib.facts.citations import GENERATED_BLOCK, is_live_doc, mask_history

# A `git show <rev>:ansible/...` pointer names the tree at a revision that held it.
_REV_POINTER = re.compile(r"\w+:ansible/\S*")
# A backticked span that names no `ansible/` path is code, such as `import ansible`.
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_WORD = re.compile(r"(?<![\w.-])[Aa]nsible(?!-(?:era|dialect|core|lint)\b)(?!\w|\.\w)")


def _ansible_offence(subject: Subject) -> list[str]:
    prose = mask_history(GENERATED_BLOCK.sub("", subject.text))
    found = []
    for n, line in enumerate(prose.splitlines(), 1):
        bare = _REV_POINTER.sub("", line)
        bare = _CODE_SPAN.sub(lambda m: m[0] if "ansible/" in m[0] else "", bare)
        if _WORD.search(bare):
            found.append(f"line {n}: {line.strip()}")
    return found


LIVE_DOCS_NAME_NO_ANSIBLE = Census(
    name="live-docs-name-no-ansible",
    reason=(
        "ADR 0029 replaced Ansible with a package per host and ADR 0030 removed the "
        "`ansible/` tree, so a live doc that names Ansible sends its reader to a path that "
        "does not exist: a recovery runbook recommended a redeploy from Ansible, and a "
        "rotation step grepped `ansible/`, where an empty result reads as no reader. Name "
        "the path that does the work (`hosts/bootstrap-host.sh`, the host package, "
        "`promote` and Flux), drop a clause that contrasts with nothing, or open a "
        "paragraph that records the past with `**HISTORY —`. The forms `Ansible-era`, "
        "`ansible-core` and a `git show <rev>:ansible/...` pointer are history already."
    ),
    files=lambda: [rel for rel in tracked("*.md") if is_live_doc(rel)],
    offence=_ansible_offence,
    red=(
        Subject("docs/a.md", "Redeploying from Ansible is the better path.\n"),
        Subject(
            "docs/a.md", "Before calling it done, grep the key across `ansible/`.\n"
        ),
        Subject("docs/a.md", "Config is Ansible-rendered.\n"),
        Subject("docs/a.md", "Run `ansible/deploy.yml` again.\n"),
    ),
    green=(
        Subject(
            "docs/a.md",
            "**HISTORY — Ansible ran from daniel-box until H7.**\nIt wrote the file.\n",
        ),
        Subject(
            "docs/a.md", "Read `git show 2460d06:ansible/roles/x/` for the rule.\n"
        ),
        Subject("docs/a.md", "The Ansible-era play stopped Compose first.\n"),
        Subject("docs/a.md", "The renderer imports no ansible-core.\n"),
        Subject("docs/a.md", "The subprocess makes `import ansible` raise.\n"),
    ),
    min_matches=80,
    must_find=frozenset({"docs/break-glass.md", "docs/k3s-etcd-restore.md"}),
)
