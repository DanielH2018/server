"""The files `decide` hands `verify`: both bundles and the record of `prod`'s statuses.

`decide` pulls `prod`'s bundle and T's before `prod` moves and writes them under
`--bundle-dir` (`write_handoff`), with the record `verify_promotion.prior_statuses` read on
`prod`'s commit, so a registry error promotes nothing. `verify` reads them back
(`read_handoff`, `read_recorded`) and makes no registry call. The rest of the handoff is
`stateful.py`'s record.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_verify_promotion.py`
"""

import json
import re
from pathlib import Path

# The statuses `decide` recorded on `prod`'s commit, at the handoff's root beside `prod/` and
# `target/`, so `read_handoff` never reads it as a bundle file.
RECORDED_FILE = "prod-statuses.json"
# The files `verify` reads from each bundle. Nothing else is handed on.
_HANDOFF_FILE = re.compile(r"digests\.json|flux/[a-z0-9][a-z0-9.-]*\.yaml")


def write_handoff(
    directory: Path,
    prod: dict[str, bytes] | None,
    target: dict[str, bytes],
    recorded: dict[str, str] | None = None,
) -> None:
    """Write the files `verify` reads from `prod`'s bundle and T's under `directory`.

    `prod` is None on the first run, which has no `prod`, and leaves `directory/prod` absent.
    `recorded` is what `prior_statuses` read on `prod`'s commit, written as RECORDED_FILE.

    Raises:
        OSError: a write failed.
    """
    for side, files in (("prod", prod), ("target", target)):
        for name, raw in (files or {}).items():
            if _HANDOFF_FILE.fullmatch(name):
                path = directory / side / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
    (directory / "target").mkdir(parents=True, exist_ok=True)
    (directory / RECORDED_FILE).write_text(
        json.dumps(recorded or {}, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_recorded(directory: Path) -> dict[str, str]:
    """The statuses `decide` recorded on `prod`'s commit, by name.

    A missing or unreadable file is no record, so every dependent stays expected: the safe
    direction.
    """
    try:
        found = json.loads((directory / RECORDED_FILE).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    if not isinstance(found, dict):
        return {}
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}


def read_handoff(
    directory: Path,
) -> tuple[dict[str, bytes] | None, dict[str, bytes]]:
    """`prod`'s files, or None when `decide` had no `prod`, and T's files.

    Raises:
        ValueError: `directory/target` does not exist, so `decide` wrote no handoff.
    """

    def files(side: Path) -> dict[str, bytes]:
        return {
            path.relative_to(side).as_posix(): path.read_bytes()
            for path in side.rglob("*")
            if path.is_file()
        }

    if not (directory / "target").is_dir():
        raise ValueError(f"{directory} holds no target/ that decide wrote")
    prod = directory / "prod"
    return (files(prod) if prod.is_dir() else None), files(directory / "target")
