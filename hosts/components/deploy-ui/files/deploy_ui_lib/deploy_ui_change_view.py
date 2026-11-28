"""The reads and caches behind `/api/changes`; `deploy_ui_changes` maps what they find to rows.

The fan-out batches come from `fanout.py board`, run in the primary checkout here and
over ssh on each host in `Config.fanout_remotes`, because an orchestrator keeps its run
manifests in its own home. They are cached for `Config.board_cache_s`, so every open tab
shares one read.
"""

import dataclasses
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from deploy_ui_lib.deploy_ui_changes import BatchRow, change_rows
from deploy_ui_lib import deploy_ui_reads as reads
from deploy_ui_lib import deploy_ui_states as states
from deploy_ui_lib.deploy_ui_reads import Unavailable

BOARD = ("uv", "run", "python", "scripts/dev/fanout.py", "board")
BOARD_TIMEOUT_S = 45


class ChangeView:
    """One row per open change, from the PR list, the landing records and the fan-out batches.

    Args:
        config: the daemon's `Config`.
        out: `App._out`, which runs an argv in the repo and raises `Unavailable`.
        prs: `App.prs`, the cached open PR list.
        landing_index: `App._landing_index`, the landing logs the daemon may read by name.
        titles: the PR titles `App` has fetched, read and never fetched here.
        now: the clock.
    """

    def __init__(
        self,
        config,
        out: Callable[..., str],
        prs: Callable[[], dict],
        landing_index: Callable[[], dict[str, Path]],
        titles: dict[str, str],
        now: Callable[[], float],
    ) -> None:
        self.cfg, self._out, self._prs, self._index = config, out, prs, landing_index
        self._titles, self.now = titles, now
        # Each branch an open PR was seen on: the merge drops the PR from `gh pr list`.
        self._branch_prs: dict[str, str] = {}
        self._board_lock = threading.Lock()
        self._board_cache: tuple[float, list[BatchRow] | None, str] | None = None
        # A landing log's (size, pr, next), so an unchanged log is not read again.
        self._results: dict[str, tuple[int, str, str]] = {}

    def changes(self) -> dict:
        """The rows, and why each source that could not be read was not.

        Each source that cannot be read is named in its own `*_unavailable` field and leaves
        the rows the others give: the PR list, the landing records, the fan-out batches.
        """
        now = self.now()
        try:
            prs, prs_unavailable = self._prs()["prs"], ""
            self._branch_prs.update({p["branch"]: str(p["number"]) for p in prs})
        except Unavailable as exc:
            prs, prs_unavailable = None, str(exc)
        try:
            table = reads.parse_proc_locks(self.cfg.proc_locks.read_text())
        except OSError:
            table = {}
        found = states.read_records(self.cfg.land_locks / states.LINE_DIR_NAME, now)
        landings = states.landing_rows(
            found.records, now, self.cfg.hostname, self.cfg.land_locks, table
        )
        batches, batches_unavailable = self._board()
        rows = change_rows(
            prs,
            landings,
            found.ended,
            batches,
            dict(self._branch_prs),
            self._land_results(),
            self._titles,
            now,
        )
        return {
            "changes": [dataclasses.asdict(r) for r in rows],
            "prs_unavailable": prs_unavailable,
            "landings_unavailable": found.unavailable,
            "batches_unavailable": batches_unavailable,
        }

    def _board(self) -> tuple[list[BatchRow] | None, str]:
        """The fan-out batches from every host's manifests, cached.

        Returns:
            The batches, or None when no read answered, and why any read or host did not
            answer; "" when all did.
        """
        with self._board_lock:
            if self._board_cache is not None:
                at, cached, why = self._board_cache
                if time.monotonic() - at < self.cfg.board_cache_s:
                    return cached, why
            sources: dict[str, list[str]] = {self.cfg.hostname: list(BOARD)}
            for host in self.cfg.fanout_remotes:
                ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host]
                sources[host] = [*ssh, f"cd {self.cfg.repo} && {' '.join(BOARD)}"]
            found: dict[tuple[str, str], BatchRow] = {}
            failed = []
            answered = False
            for where, argv in sources.items():
                try:
                    raw = json.loads(self._out(argv, BOARD_TIMEOUT_S))
                    rows = [BatchRow.parse(r) for r in raw]
                except (Unavailable, KeyError, TypeError, ValueError) as exc:
                    failed.append(f"{where}'s manifests: {str(exc)[:200]}")
                    continue
                answered = True
                found.update({(r.host, r.batch): r for r in rows})
            unread = sorted({r.host for r in found.values() if r.running is None})
            failed += [f"{host} did not answer for its batches" for host in unread]
            result = (list(found.values()) if answered else None, "; ".join(failed))
            self._board_cache = (time.monotonic(), *result)
            return result

    def _land_results(self) -> dict[str, str]:
        """Each PR's `next` action from the newest landing log the index holds for it."""
        newest: dict[str, tuple[float, str]] = {}
        index = self._index()
        self._results = {n: m for n, m in self._results.items() if n in index}
        for name, path in index.items():
            try:
                st = path.stat()
                memo = self._results.get(name)
                if memo is None or memo[0] != st.st_size:
                    fields = reads.land_result(reads.read_log(path)[0])
                    memo = (st.st_size, fields.get("pr", ""), fields.get("next", ""))
                    self._results[name] = memo
            except OSError:
                continue
            _, pr, next_action = memo
            if pr and next_action and st.st_mtime > newest.get(pr, (0.0, ""))[0]:
                newest[pr] = (st.st_mtime, next_action)
        return {pr: n for pr, (_, n) in newest.items()}
