# ansible/roles/setup/gitops_deploy/files/gitops_hold.py
"""The deployer's hold: `hold_sha`, the planes it waits on, and the rule that clears it (#3658).

A failed broad apply holds its SHA and records the plane it failed in as an `owed` ledger
line of class `hold_plane`. An apply that covers a plane drops that line, and `hold_sha`
clears once no plane is left (#878). The rule is that `hold_sha` clears only together with its
`hold_plane` lines, and `Hold` is its one owner. The deployer reaches it through
`deploy_state_hold.HoldMarkers`, the deploy UI's Clear calls `Hold.clear`, and each used to
restate the file layout, the separator and the rule by hand.

Shipped with the deployer (`tasks/code.yml`) and installed by path beside deploy-ui's
`gitops_ledger`, which is why it is stdlib only plus `gitops_markers` and `gitops_ledger`:
the deploy UI runs outside the repo venv and can import nothing else. `gitops_ledger` must
never import this module, because monitor-bridge carries a copy of the ledger and not of this.

`Hold` reads and writes the two files itself rather than through `DeployerState`, for the
same reason. It writes the way `host_lib.atomic_write` does (temp file, then `os.replace`), and
decodes with `surrogateescape` so a torn byte in another class's ledger line survives a
rewrite unchanged.
"""

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import ContextManager

from gitops_ledger import (
    OWED_HOLD_PLANE,
    drop_owed,
    held_planes,
    owed_line,
    owed_line_key,
    parse_owed,
    rewrite_owed,
)
from gitops_markers import MARKERS

# Between the held planes on one line. Not a newline: every reader prints them on one line.
HOLD_PLANE_SEP = "; "

# Between a role tag and one of its block tags in a held tag (#3138). No Ansible tag in
# `initial_setup.yml` or under `roles/setup/` contains one, so a held tag splits unambiguously.
HELD_ROLE_SEP = ":"


def hold_plane_marker(playbook: str, tags: list[str] | None) -> str:
    """The `hold_plane` subject naming a broad apply: the playbook, then its tags.

    One definition so the writer and `broad_hold_cleared_by` cannot disagree on the format.
    """
    return f"{playbook} {','.join(tags or [])}".strip()


def held_tag(role_tag: str, tag: str) -> str:
    """A held tag that remembers the role it was narrowed from: `<role tag>:<block tag>`.

    A narrowed setup apply runs block tags, and a later whole-role apply (`--tags <role>`)
    reruns every one of them. A bare `gitops-config` in the subject cannot say which role
    tag covers it, so `broad_hold_cleared_by` reads the qualified form. A tag that IS the
    role tag stays bare, which is also every hold written before #3138.
    """
    return tag if tag == role_tag else f"{role_tag}{HELD_ROLE_SEP}{tag}"


def _held_tag_covered(held: str, applied: set[str]) -> bool:
    """Did an apply of the tags `applied` run the held tag `held`?

    `<role>:<block>` is covered by the block tag itself or by its whole-role tag, which
    selects every block in the role. A bare tag is covered only by itself.
    """
    role, sep, block = held.partition(HELD_ROLE_SEP)
    if not sep:
        return held in applied
    return block in applied or role in applied


def broad_hold_cleared_by(held: str, playbook: str, tags: list[str] | None) -> bool:
    """Does a successful apply of `playbook`/`tags` cover the plane recorded in `held`?

    A broad hold says one plane is unapplied. Clearing it on a success in a DIFFERENT plane
    throws away the fact it records: on 2026-09-02 a failed `ansible/deploy.yml` held
    `2d25ced3`, the next tick applied the setup plane, and both markers were gone within 30
    seconds while the deploy plane stayed unapplied (issue #878). Every consumer —
    `checks.gitops.gitops_status`, `land.sh`, `renovate_agent.decide` — gates on `hold_sha`,
    so the erasure also turned **GitOps Deploy — Status** green over that unapplied plane.

    Coverage, not equality, and it runs both ways round. An untagged run applies the whole
    playbook, so it covers any tag set held against it; a tagged run covers a held tag set it
    is a superset of, and covers an UNTAGGED hold not at all — that hold names the whole
    playbook, of which a tagged run applies one part. An empty `held` means nothing is held.

    A held `<role>:<block>` tag (`held_tag`) is covered by that block or by the role's own
    tag (#3138). A narrowed setup apply therefore holds only the blocks it ran: the role's
    whole-role apply clears it, and a narrowed apply of a DIFFERENT block of that role does
    not.
    """
    if not held.strip():
        return True
    held_playbook, _, held_tags = held.strip().partition(" ")
    if held_playbook != playbook:
        return False
    applied = set(tags or [])
    if not applied:
        return True
    wanted = {t for t in held_tags.split(",") if t}
    return bool(wanted) and all(_held_tag_covered(t, applied) for t in wanted)


def _held_subjects(owed: str | None) -> list[str]:
    """Every subject a `hold_plane` ledger line names, torn lines included, in file order.

    A TORN line counts. The hold-clear rule reads this list, and a plane it skipped would
    let `hold_sha` clear while that plane is still unapplied (#878's class).
    """
    keys = (owed_line_key(line) for line in (owed or "").splitlines())
    subjects = [k[1].strip() for k in keys if k and k[0] == OWED_HOLD_PLANE]
    return list(dict.fromkeys(subjects))


class Hold:
    """The hold under one state directory: `hold_sha` and the ledger's `hold_plane` lines.

    No method takes the git-tree lock unless it is handed one. Every deployer call runs inside
    a tick, which already holds it; `clear` is the one call made from outside a tick, and it
    always takes the lock it is handed.

    Attributes:
        state_dir: the deployer's state directory, `gitops_markers.STATE_DIR` on a host.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    def _path(self, marker: str) -> Path:
        return self.state_dir / MARKERS[marker]

    def _read(self, marker: str) -> str | None:
        """The marker's stripped contents, or None when it is absent or empty.

        Raises:
            OSError: the file exists but could not be read. An unreadable state directory is
                NOT "no hold", so the error propagates, as `DeployerState.read` lets it.
        """
        try:
            text = self._path(marker).read_text(errors="surrogateescape")
        except FileNotFoundError:
            return None
        return text.strip() or None

    def _write(self, marker: str, value: str | None) -> None:
        """Set the marker atomically, or remove it when `value` is None or empty."""
        path = self._path(marker)
        if not value:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(value, errors="surrogateescape")
        os.replace(tmp, path)

    # ── reads ─────────────────────────────────────────────────────────────────────────────

    def current(self) -> str | None:
        """The commit this host refuses to redeploy, or None."""
        return self._read("hold")

    def planes(self) -> list[str]:
        """Every readable plane the hold waits on, oldest first (`gitops_ledger.held_planes`)."""
        return held_planes(self._read("owed"))

    def held_subjects(self) -> list[str]:
        """Every plane a `hold_plane` line names, torn lines included, in file order."""
        return _held_subjects(self._read("owed"))

    # ── the deployer's writes, each made under the tick's tree lock ───────────────────────

    def set_sha(self, sha: str | None) -> None:
        """Record `sha` as held with no plane, or remove `hold_sha` when `sha` is None.

        A k8s rollback holds this way: its way out is a service deploy, which `cover_services`
        clears. A `None` leaves every `hold_plane` line in place, so it is not a clear; the
        operator's clear is `clear`.
        """
        self._write("hold", sha)

    def record(self, sha: str, playbook: str, tags: list[str]) -> None:
        """Hold `sha` for a failed apply of `playbook`/`tags`, beside any plane already held.

        Added to the ledger's `hold_plane` class, never written over an entry already there.
        A second failure used to OVERWRITE the plane, so the first plane's entry was gone
        while that plane was still unapplied: the second failure's fix then cleared
        `hold_sha` over it, and GitOps Deploy — Status read green (#878's class). Each entry
        clears on its own. An entry already listed keeps its origin and first-seen stamp.
        """
        now = time.time()
        owed = self._read("owed")
        self.set_sha(sha)
        entry = hold_plane_marker(playbook, tags)
        # `rewrite_owed` repairs a TORN line naming the entry (#2657) rather than leaving it
        # beside a second, readable one.
        text = rewrite_owed(owed, OWED_HOLD_PLANE, [entry], sha, now, advance=False)
        if entry not in {e.subject for e in parse_owed(text, OWED_HOLD_PLANE)}:
            text = "\n".join(
                [*text.splitlines(), owed_line(OWED_HOLD_PLANE, entry, sha, now)]
            )
        if text != (owed or ""):
            self._write("owed", text)

    def cover(self, playbook: str, tags: list[str]) -> list[str]:
        """Drop each plane a successful apply of `playbook`/`tags` covers; clear once none is left.

        Returns:
            The planes still held, in file order. Empty means `hold_sha` is clear.

        The covered entries go BEFORE `hold_sha`. A crash between the two leaves a hold
        with fewer planes, which the next apply clears, rather than planes with no hold.
        """
        owed = self._read("owed")
        held = _held_subjects(owed)
        left = [e for e in held if not broad_hold_cleared_by(e, playbook, tags)]
        if left != held:
            text, _ = drop_owed(owed, OWED_HOLD_PLANE, set(held) - set(left))
            self._write("owed", text)
        if not left:
            self._write("hold", None)
        return left

    def cover_services(self, services: set[str]) -> list[str]:
        """`cover` for a successful k8s deploy of `services`, which is `deploy.yml --tags`.

        An EMPTY set covers nothing. `cover` would read it as an untagged `deploy.yml`, which
        covers every `deploy.yml` plane, so a deploy of no service would clear over planes
        nothing applied.
        """
        if not services:
            held = self.held_subjects()
            if held:
                return held
        return self.cover("ansible/deploy.yml", sorted(services))

    # ── the operator's clear ──────────────────────────────────────────────────────────────

    def clear(
        self,
        expected_sha: str,
        lock: Callable[[], ContextManager[object]] = contextlib.nullcontext,
    ) -> str | None:
        """Remove `hold_sha` and every `hold_plane` line, only when `expected_sha` is held.

        Args:
            expected_sha: the SHA the operator typed. A different live hold refuses.
            lock: a factory for the git-tree lock, which every tick holds while it writes the
                hold. A caller outside a tick passes the lock; it may raise to refuse, and the
                hold is then left whole.

        Returns:
            None on success, else the refusal text for a SHA mismatch.

        The check before the lock only saves a wait on a stale page. The comparison that
        decides is the one inside it, and the unlink is inside it too (#3755). A tick that
        failed after the first comparison has written its own `hold_sha` and `hold_plane`
        line by the time the lock is free, so clearing on the first comparison alone dropped
        a hold the operator never typed.

        The lock is taken even when no `hold_plane` line exists. A tick can add one between
        the read and the unlink, which would leave a plane with no hold. The cost is that a
        Clear during a tick refuses, through the lock factory, until the tick ends.
        """
        refusal = self._mismatch(expected_sha)
        if refusal:
            return refusal
        with lock():
            refusal = self._mismatch(expected_sha)
            if refusal:
                return refusal
            owed = self._read("owed")
            # Raw subjects, not `held_subjects`' stripped ones, which `drop_owed` would fail
            # to match on a subject carrying stray whitespace.
            keys = map(owed_line_key, (owed or "").splitlines())
            held = {k[1] for k in keys if k and k[0] == OWED_HOLD_PLANE}
            # The planes go first, so a crash between the writes leaves a hold with no plane
            # rather than planes with no hold.
            if held:
                text, _ = drop_owed(owed, OWED_HOLD_PLANE, held)
                self._write("owed", text)
            self._write("hold", None)
        return None

    def _mismatch(self, expected_sha: str) -> str | None:
        """The refusal text when the live hold is not `expected_sha`, else None."""
        live = self.current() or ""
        if live != expected_sha:
            return f"hold is {live or 'clear'}, not {expected_sha}; reload and retry"
        return None
