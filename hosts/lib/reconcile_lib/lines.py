"""The line arm: one anchored line, or one marker-delimited block, in a file dpkg does not overwrite.

The design is docs/target-architecture/host/h7-delete-ansible.md, *Reconcilers for state dpkg
does not hold*. A host package's manifest lists "lines" in `reconcilers` and carries:

    "lines": {"edits": [
        {"path": "/etc/sysctl.conf", "line": "vm.swappiness=10", "regexp": "^\\s*vm\\.swappiness\\s*="},
        {"path": "/etc/zsh/zshenv", "block": "export X=1", "marker": "# {mark} MANAGED BLOCK"}]}

A line edit has the contract of Ansible's `lineinfile` with a `regexp`, and a block edit that of
`blockinfile`. The regexp and the markers equal the Ansible task's, so the first run adopts the
line or block Ansible wrote and adds no second copy. An edit may carry `mode`, the octal mode of a
file the arm has to create. Rewriting an existing file keeps its owner and mode, because sshd's
`StrictModes` rejects an authorized_keys file the arm left with the wrong ones.

Before it writes, the arm runs `dpkg -S` on the path. It accepts an unowned file, and an owned file
only when every owner lists the path among its conffiles. It refuses any other owned file, so it
can never fight dpkg, and a refusal is a fault: the package shipped something wrong. The arm never
removes a line, so it keeps no record and reports no drift.

`activate:` on an edit is not read here. The renderer writes it into the manifest's `activations`
map, and step 3b runs the action when the file's hash changes.
"""

import os
import re
import tempfile
from pathlib import Path

from reconcile_lib.common import under_root, run, say

KEYS = ({"path", "line", "regexp"}, {"path", "block", "marker"})
MODE = re.compile(r"[0-7]{3,4}")


def valid_edit(edit: object) -> bool:
    if not isinstance(edit, dict) or set(edit) - {"mode"} not in KEYS:
        return False
    path, mode = edit["path"], edit.get("mode", "0644")
    text = edit.get("line", edit.get("block"))
    anchor = edit.get("regexp", edit.get("marker"))
    if not (
        isinstance(path, str)
        and path.startswith("/")
        and ".." not in path.split("/")
        and isinstance(text, str)
        and text
        and isinstance(anchor, str)
        and isinstance(mode, str)
        and MODE.fullmatch(mode)
    ):
        return False
    if "block" in edit:
        return "{mark}" in anchor and "\n" not in anchor
    try:
        # A line its own regexp misses would be appended again on every run.
        return "\n" not in text and re.search(anchor, text) is not None
    except re.error:
        return False


def valid(section: object) -> bool:
    """True when `section` is a well-formed manifest `lines` object."""
    return (
        isinstance(section, dict)
        and set(section) == {"edits"}
        and isinstance(section["edits"], list)
        and all(map(valid_edit, section["edits"]))
    )


def refusal(path: str) -> str | None:
    """Why the arm must not edit `path`, or None when no package owns it or only a conffile."""
    found = run("dpkg", "-S", path)
    if found.returncode == 1:
        return None
    owners = set()
    for line in found.stdout.splitlines():
        head, _, tail = line.rpartition(": ")
        if tail == path and not head.startswith("diversion"):
            owners.update(owner.strip() for owner in head.split(","))
    if found.returncode != 0 or not owners:
        return f"dpkg -S {path} failed: {found.stderr.strip()}"
    for owner in sorted(owners):
        listed = run("dpkg-query", "-W", "-f", "${Conffiles}\\n", owner).stdout
        if path not in (row.split()[0] for row in listed.splitlines() if row.strip()):
            return f"{path} is owned by {owner}, which does not list it as a conffile"
    return None


def with_line(text: str, edit: dict) -> str:
    """`text` with the last line the regexp matches replaced, or the line appended."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if re.search(edit["regexp"], line)]
    if hits:
        lines[hits[-1]] = edit["line"] + "\n"
    else:
        lines += ["\n"] if text and not text.endswith("\n") else []
        lines.append(edit["line"] + "\n")
    return "".join(lines)


def with_block(text: str, edit: dict) -> str:
    """`text` with the marked block replaced, or the block appended after the last line."""
    begin, end = (edit["marker"].replace("{mark}", word) for word in ("BEGIN", "END"))
    block = f"{begin}\n{edit['block'].rstrip(chr(10))}\n{end}\n"
    lines = text.splitlines(keepends=True)
    names = [line.rstrip("\n") for line in lines]
    if begin in names and end in names[names.index(begin) :]:
        first = names.index(begin)
        last = names.index(end, first)
        return "".join(lines[:first]) + block + "".join(lines[last + 1 :])
    return text + ("\n" if text and not text.endswith("\n") else "") + block


def write(target: Path, text: str, mode: str) -> None:
    """Replace `target` atomically, keeping an existing file's owner and mode."""
    existing = target.stat() if target.exists() else None
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(tmp, existing.st_mode & 0o7777 if existing else int(mode, 8))
    if existing and (existing.st_uid, existing.st_gid) != (os.getuid(), os.getgid()):
        os.chown(tmp, existing.st_uid, existing.st_gid)
    os.replace(tmp, target)


def apply(edit: dict) -> bool:
    problem = refusal(edit["path"])
    if problem:
        say(f"lines: {problem}; refusing the edit")
        return False
    target = under_root(Path(edit["path"]))
    try:
        old = target.read_text() if target.exists() else ""
        new = (with_block if "block" in edit else with_line)(old, edit)
        if new != old:
            write(target, new, edit.get("mode", "0644"))
            say(f"lines: edited {edit['path']}")
    except (OSError, UnicodeDecodeError) as exc:
        say(f"lines: cannot edit {edit['path']}: {exc}")
        return False
    return True


def reconcile(section: dict | None, prev: dict | None) -> tuple[bool, None, int]:
    """Run the arm. Return whether every edit applied; it keeps no record and counts no drift."""
    edits = section["edits"] if section else []
    return all([apply(edit) for edit in edits]), None, 0
