"""The `fanout-headroom` source for cc-wait: whether `fanout.py place launch` would place a batch.

`cc-wait fanout-headroom [--batches N] [--host HOST]` runs this through
`.claude/wait-sources/fanout-headroom`. It answers the question an orchestrator refilling a
fan-out asks between launches, "would a launch place now?", which `cc-wait fanout <run-id>`
answers only for the runs it names.

    fanout.py headroom --describe                 # terminal states, as JSON
    fanout.py headroom [--batches N] [--host H]   # the state now, one JSON line

ONE DEFINITION OF A PLACEMENT. This reads the hosts with `place.candidate_readings` and
decides with `place.placement`, the two calls `launch` makes, over `--batches` placeholder
names. So `room` here and `launch`'s exit 3 cannot disagree on memory headroom, the live-batch
cap, the ssh budget or the signing gate. The gates that read issue bodies (labels, solo-only,
shared files, a live claim) are not read: no wait clears them.

THE STATE.

    full             (open)  launch would place nothing; the detail is launch's own reason
    room             0       launch would place every one of the N batches
    no-signing-host  6       no readable host signs commits GitHub verifies

`full` follows launch where launch is stricter than it needs to be. `placement.place` picks
the host with the most memory headroom, and `over_live_batch_cap` then refuses when that host
is at its batch cap, without trying the other host. An unpinned `full` therefore names each
host a `--host` launch would place on.

A host that cannot be read drops out, as in launch, and is named in the detail. No readable
host, or an unreadable GitHub signing-key list, is a failed read: the probe exits 1 and
cc-wait retries it.

A REMOTE SOURCE. Each read spends one ssh connection to daniel-server and one `gh api` call,
so this declares `remote` and a 120s interval. The ssh connection goes through the
transport's control socket (`transport.mux_opts`), which a `launch` started within
`MUX_PERSIST_S` reuses rather than opening another against `ufw limit ssh`.
"""

import contextlib
import io
import json
import sys
from pathlib import Path

# Reach `fanout_lib` (scripts/dev) and `lib` (scripts/): a directly-invoked script gets only
# its own directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib import manifest as manifest_mod
from fanout_lib import place
from fanout_lib.placement import HostReading
from fanout_lib.transport import HOSTS, Tools, registered_keys
from lib.cli_help import ArgumentParser, refuse_help_under_describe
from lib.exit_codes import FANOUT_PLACE_NO_SIGNING_HOST

TERMINAL = {"room": 0, "no-signing-host": FANOUT_PLACE_NO_SIGNING_HOST}


class Unreadable(Exception):
    """No host, or the account's signing keys, could be read, so there is no verdict."""


def _messages(stderr: str, kind: str) -> list[str]:
    """The text after `: <kind>: ` on each line `lib.cli_help.error` or `warn` printed."""
    marker = f": {kind}: "
    return [ln.split(marker, 1)[1] for ln in stderr.splitlines() if marker in ln]


def _decide(
    names: list[str],
    good: list[HostReading],
    pin: str | None,
    root: Path,
    multiplexed: bool,
) -> tuple[list[tuple[str, str]] | None, list[str]]:
    """`place.placement`'s answer, with the errors it printed instead of printing them."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        placed = place.placement(names, good, pin, root, multiplexed)
    return placed, _messages(err.getvalue(), "error")


def read(
    tools: Tools, batches: int, pin: str | None, root: Path = manifest_mod.MANIFEST_DIR
) -> dict:
    """Whether a launch of `batches` batches would place, as cc-wait's state and detail.

    Args:
        tools: the process boundary.
        batches: how many batches the launch would carry.
        pin: the host `--host` would pin the launch to, or None to let placement choose.
        root: the manifest directory the live-batch cap counts.

    Raises:
        Unreadable: no host could be read, or the registered signing keys could not.
    """
    registered = registered_keys(tools)
    if isinstance(registered, str):
        raise Unreadable(registered)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        good = place.candidate_readings(
            tools, registered, [pin] if pin else list(HOSTS), root
        )
    warnings = _messages(err.getvalue(), "warning")
    if good == FANOUT_PLACE_NO_SIGNING_HOST:
        detail = " | ".join(_messages(err.getvalue(), "error") + warnings)
        return {"state": "no-signing-host", "detail": detail[:600]}
    if isinstance(good, int):
        raise Unreadable(" | ".join(warnings) or "no host could be read")
    names = [str(n) for n in range(1, batches + 1)]
    placed, errors = _decide(names, good, pin, root, tools.multiplexed())
    if placed is not None:
        where = ", ".join(sorted({host for _, host in placed}))
        detail = f"{batches} batch(es) would place on {where}"
        return {"state": "room", "detail": "; ".join([detail, *warnings])[:600]}
    if not pin:
        # Launch tries only the host with the most memory headroom against the batch cap;
        # a pinned launch can still place on the other one.
        pinnable = [
            r.host
            for r in good
            if _decide(names, good, r.host, root, tools.multiplexed())[0] is not None
        ]
        errors += [f"`--host {host}` would place" for host in pinnable]
    return {"state": "full", "detail": " | ".join(errors + warnings)[:600]}


def describe() -> dict:
    return {"terminal": TERMINAL, "interval_s": 120, "remote": True}


def main(argv: list[str] | None = None, tools: Tools | None = None) -> int:
    parser = ArgumentParser(
        prog="fanout.py headroom", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--describe", action="store_true")
    parser.add_argument(
        "--batches",
        type=int,
        default=1,
        help="how many batches the launch would carry (default 1)",
    )
    parser.add_argument(
        "--host", choices=HOSTS, help="the host a `--host` launch would pin to"
    )
    refuse_help_under_describe(parser, argv)
    ns = parser.parse_args(argv)
    if ns.batches < 1:
        parser.error("--batches must be at least 1")
    if ns.describe:
        print(json.dumps(describe()))
        return 0
    try:
        print(json.dumps(read(tools or Tools(), ns.batches, ns.host)))
    except Unreadable as exc:
        print(f"fanout_headroom: {exc}", file=sys.stderr)
        return 1
    return 0
