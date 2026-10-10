# ansible/roles/setup/gitops_deploy/files/deploy_setup_roles.py
"""Which playbook and `--tags` value apply a setup role, and whether the tick's run applies it.

The playbooks are the one list (#3734). `scripts/deploy_tools/setup_routing.py` derives the
routing from `initial_setup.yml`, `k3s-bringup.yml` and `bootstrap.yml`, evaluating each
entry's `when:` against the tick host's vars. This unit runs under `uv run --no-project` and
cannot import yaml, so the tick runs that script as a subprocess over origin's tree
(`derive_routing`) and installs the result with `use_routing`. A repo-env process that
installs nothing derives from its own checkout on first use.

THE BUG THIS EXISTS TO KILL: `--tags` matching no task makes Ansible exit 0, so a guessed
playbook or tag records an apply of nothing (PR #702; `docs/gitops-pipeline.md`, *Broad
changes*). So a role the routing does not place — the subprocess failed, timed out, or could
not read the role's entry — is never guessed at. `tick_applies_setup_role` is False for it,
and a range carrying it parks without a fast-forward, so the next tick derives again
(`deploy_defer.parks_the_tick`).

Split out of `deploy_changes` at the module-length cap. `deploy_changes` re-exports every
name, so its readers keep their imports.
"""

from __future__ import annotations

import json
import subprocess
from typing import NamedTuple

INITIAL_SETUP = "ansible/initial_setup.yml"

ROUTING_SCRIPT = "scripts/deploy_tools/setup_routing.py"
# Three playbooks and the vars files, parsed once: 0.8s measured under a loaded suite run.
# Thirty seconds is the wedged case, as for the setup narrowing (`NARROW_SETUP_TIMEOUT_S`).
ROUTING_TIMEOUT_S = 30.0


class SetupRoute(NamedTuple):
    """How one setup role is applied.

    Attributes:
        playbook: the playbook that applies it, or None when no playbook does (`common`).
        tag: the `--tags` value that selects it, which is not always its name.
        on_tick_host: whether its `when:` admits the tick's host.
        host: the one host its gate admits, for a role the tick's host does not run, else
            None. The printed remediation appends `-e target=<host>` from it.
    """

    playbook: str | None
    tag: str
    on_tick_host: bool
    host: str | None


# None until a tick installs routing or a reader first asks; see `routing`.
_ROUTES: dict[str, SetupRoute] | None = None


def routing_argv(ref: str, host: str) -> list[str]:
    """The command `derive_routing` runs, split out so a test can hand it to the script."""
    return [
        "uv",
        "run",
        "--frozen",
        "python",
        ROUTING_SCRIPT,
        "--ref",
        ref,
        "--host",
        host,
    ]


def routes_from_json(text: str) -> tuple[dict[str, SetupRoute], dict[str, str]]:
    """The script's stdout as (routes, unplaced role -> why).

    Raises:
        ValueError: the text is not the script's JSON shape. A json.JSONDecodeError is one.
    """
    data = json.loads(text)
    try:
        routes = {role: _route(role, r) for role, r in data["routes"].items()}
        return routes, dict(data["unplaced"])
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"not the routing JSON: {exc!r}") from exc


def _route(role: str, r: dict) -> SetupRoute:
    playbook, tag, host = r["playbook"], r["tag"], r["host"]
    if not isinstance(tag, str) or not isinstance(r["on_tick_host"], bool):
        raise ValueError(f"{role}: malformed route {r!r}")
    if playbook is not None and not isinstance(playbook, str):
        raise ValueError(f"{role}: malformed playbook {playbook!r}")
    if host is not None and not isinstance(host, str):
        raise ValueError(f"{role}: malformed host {host!r}")
    return SetupRoute(playbook, tag, r["on_tick_host"], host)


def derive_routing(
    repo: str, ref: str, host: str
) -> tuple[dict[str, SetupRoute], dict[str, str]]:
    """Run `setup_routing.py` over `ref`'s tree in `repo`, for the tick host `host`.

    Returns:
        (routes, unplaced role -> why), as `routes_from_json` reads them.

    Raises:
        subprocess.TimeoutExpired: the child outlived `ROUTING_TIMEOUT_S`.
        RuntimeError: the child exited non-zero; the message is its stderr.
        ValueError: its stdout is not the routing JSON.
        OSError: the child could not be started.
    """
    r = subprocess.run(
        routing_argv(ref, host),
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=ROUTING_TIMEOUT_S,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"exited {r.returncode}")
    return routes_from_json(r.stdout)


def current_routing() -> dict[str, SetupRoute] | None:
    """What `use_routing` last installed, None if nothing; a test restores it with this."""
    return _ROUTES


def use_routing(routes: dict[str, SetupRoute] | None) -> None:
    """Route setup roles by `routes` for the rest of the process.

    `{}` routes nothing, which is the tick's refusal when the derivation failed. None goes
    back to deriving from the checkout on the next read.
    """
    global _ROUTES
    _ROUTES = routes


def routing() -> dict[str, SetupRoute]:
    """The installed routes, or the checkout's when a repo-env reader installed none.

    The deployer's own directory holds no `setup_routing`, so a tick that installed nothing
    routes nothing. A `scripts/deploy_tools` process has it beside its own modules.
    """
    global _ROUTES
    if _ROUTES is None:
        try:
            import setup_routing
        except ImportError:
            _ROUTES = {}
        else:
            routes, _ = routes_from_json(json.dumps(setup_routing.routes()))
            _ROUTES = routes
    return _ROUTES


def roles_outside_initial_setup_in(playbook: str) -> set[str]:
    """The setup roles `playbook` applies that `initial_setup.yml` does not.

    Empty for `initial_setup.yml` itself and for `bootstrap.yml`: neither carries a role routed
    to it, so a change to either is named as a run of the whole playbook.
    """
    if playbook == INITIAL_SETUP:
        return set()
    return {role for role, r in routing().items() if r.playbook == playbook}


def is_routed(role: str) -> bool:
    """Whether the routing places `role`; False for one it could not read, or with none."""
    return role in routing()


def setup_role_playbook(role: str) -> str | None:
    """The playbook that applies a setup role, or None when none does or it is unplaced."""
    route = routing().get(role)
    return route.playbook if route else None


def tick_applies_setup_role(role: str) -> bool:
    """Whether the tick's own `initial_setup.yml --tags <tag>` run applies `role`."""
    route = routing().get(role)
    return bool(route and route.playbook == INITIAL_SETUP and route.on_tick_host)


def tag_selects_an_off_host_role(tag: str) -> bool:
    """Whether `--tags <tag>` selects an `initial_setup.yml` role gated off the tick's host.

    Keyed by the tag, not the role directory: `chezmoi_setup` is selected by `chezmoi`.
    """
    return any(
        r.tag == tag and r.playbook == INITIAL_SETUP and not r.on_tick_host
        for r in routing().values()
    )


def setup_role_tag(role: str) -> str:
    """The `--tags` value that actually selects a setup role, which is not always its name.

    An unplaced role reads as its own name. Nothing applies by that value: the role is the
    tick's to record, and the name is what its ledger line and clear command print.
    """
    route = routing().get(role)
    return route.tag if route else role


def setup_role_host(role: str) -> str | None:
    """The one host a role the tick's host does not run is gated onto, else None."""
    route = routing().get(role)
    return route.host if route else None
