"""The census row requiring `sudo` on a runbook's fenced `k3s` command."""

import re

from _row_table import Census, Subject, tracked

_FENCE = re.compile(r"^\s*(```|~~~)")
_K3S_COMMAND = re.compile(r"^\s*(?:\$ )?k3s [a-z]")
_ROOT_SHELL = re.compile(r"^\s*sudo -[is]\b")


def _bare_k3s_offence(subject: Subject) -> list[str]:
    offences = []
    in_block = False
    rooted = False
    for number, line in enumerate(subject.text.splitlines(), 1):
        if _FENCE.match(line):
            in_block = not in_block
            rooted = False
        elif in_block and _ROOT_SHELL.match(line):
            rooted = True
        elif in_block and not rooted and _K3S_COMMAND.match(line):
            offences.append(f"line {number} runs k3s without sudo")
    return offences


def _k3s_command_count(subject: Subject) -> int:
    in_block = False
    count = 0
    for line in subject.text.splitlines():
        if _FENCE.match(line):
            in_block = not in_block
        elif in_block and re.match(r"^\s*(?:sudo )?k3s [a-z]", line):
            count += 1
    return count


RUNBOOK_K3S_COMMANDS_CARRY_SUDO = Census(
    name="runbook-k3s-commands-carry-sudo",
    reason=(
        "The operator runs as `ubuntu`, and every `k3s` subcommand reads root-only files under "
        "/var/lib/rancher/k3s/, so a bare one pasted from a runbook fails with `open "
        "/home/ubuntu/.rancher/k3s/server/token: no such file or directory`. Write `sudo k3s "
        "...`, or open the block with `sudo -i` when the step holds a root shell."
    ),
    files=lambda: [
        rel for rel in tracked("docs/*.md") if not rel.startswith("docs/archive/")
    ],
    offence=_bare_k3s_offence,
    count=_k3s_command_count,
    red=(
        Subject("a.md", "```bash\nk3s etcd-snapshot save\n```\n"),
        Subject(
            "a.md",
            "```bash\nsudo -i\n```\n\n```bash\nk3s server --cluster-reset\n```\n",
        ),
    ),
    green=(
        Subject("a.md", "```bash\nsudo k3s etcd-snapshot save\n```\n"),
        Subject("a.md", "```bash\nsudo -i\nk3s etcd-snapshot save\n```\n"),
        Subject("a.md", "k3s etcd-snapshot save runs on the host.\n"),
    ),
    must_find=frozenset({"docs/k3s-etcd-restore.md", "docs/host-packages.md"}),
)
