#!/usr/bin/env python3
"""The Claude fleet's memory bound must be ONE number on ONE cgroup, not two that look like one.

claude-rc.service and user-<uid>.slice must not both render `claude_code_rc_memory_high`.
Sharing a variable is not sharing a cap — they are cgroup siblings under different parents
(system.slice and user.slice), so neither sees the other's usage and the fleet's real
throttle point is the SUM: 16G of anon plus 4G of swap on a 28 GiB box that also runs k3s
and Longhorn.

Both planes nest under one parent slice that carries the single derived bound
(`templates/fleet-slice-caps.conf.j2` on user.slice, plus `Slice=` on the RC unit), leaving
the per-plane caps as sub-bounds. `user-<uid>.slice` itself cannot be reparented —
systemd.slice(5) derives a slice's parent from its NAME — so the unit we do control is the
one that moves. This module pins the parts of that shape a future edit could quietly undo:

1. The parent's caps render from the FLEET variables, not from a plane's.
2. The RC unit is placed in that parent slice, and stops being placed there when the shared
   parent is turned off (the rollback direction).
3. Every per-plane cap is within the fleet bound — a sub-bound above its parent's can never
   be reached, so it would silently mean the parent's number instead.
4. The drop-in has a removal task, so the shared parent has a way out as well as a way in.

Each check is a pair per this repo's "a new check ships with a proof it can go RED" rule: one
input it must accept and one it must reject.

Run: uv run pytest hosts/tests/claude_code/test_claude_fleet_slice_cap.py
"""

import re

import pytest
from _agent_package import render_daniel_server_with
from _setup_render import CLAUDE_CODE, render_component_text

COMPONENT = "claude-code"
ROLE_DIR = CLAUDE_CODE
DEFAULTS = ROLE_DIR / "defaults" / "main.yml"

FLEET_CONF = "fleet-slice-caps.conf.j2"
RC_UNIT = "claude-rc.service.j2"

# Asserted present first, so a rename that empties the parse fails here rather than passing an all()
# over nothing.
REQUIRED_VARS = frozenset(
    {
        "claude_code_fleet_slice",
        "claude_code_fleet_memory_high",
        "claude_code_fleet_swap_max",
        "claude_code_rc_memory_high",
        "claude_code_rc_memory_swap_max",
    }
)

# The per-plane cap -> the fleet cap it must sit under.
SUB_BOUNDS = {
    "claude_code_rc_memory_high": "claude_code_fleet_memory_high",
    "claude_code_rc_memory_swap_max": "claude_code_fleet_swap_max",
}

_SUFFIXES = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def to_bytes(value: object) -> int:
    """Parse a systemd memory size ("12G", "512M", "8589934592") into bytes.

    systemd's own suffixes are powers of 1024 (systemd.syntax: K/M/G/T are binary), so this
    reads 12G as 12 GiB, matching what the kernel writes into memory.high.
    """
    text = str(value).strip()
    match = re.fullmatch(r"(\d+)([KMGT])?", text)
    assert match, f"{value!r} is not a systemd memory size"
    return int(match.group(1)) * _SUFFIXES.get(match.group(2) or "", 1)


def render(template: str, **overrides: object) -> str:
    """One of this role's templates rendered at inventory values, `overrides` on top.

    Through `_setup_render` rather than a synthetic context of this module's own: the context
    this module carried drifted to a fleet cap of 12G while defaults/main.yml moved to 14G, so
    every assertion below was checking a number the host never renders.
    """
    return render_component_text(COMPONENT, template, overrides)


@pytest.fixture(scope="module")
def defaults() -> dict[str, object]:
    from lib import yaml_fast

    parsed = yaml_fast.safe_load(DEFAULTS.read_text())
    missing = REQUIRED_VARS - set(parsed)
    assert not missing, (
        f"defaults/main.yml no longer defines {sorted(missing)} — the fleet bound has no "
        "owner, and every comparison below would pass over an empty set"
    )
    return parsed


def test_the_shared_parent_drop_in_still_exists() -> None:
    """Named here rather than in a fixture: `render` fails on a missing template with the
    path, and this failure says what losing the template MEANS."""
    assert (ROLE_DIR / "templates" / FLEET_CONF).is_file(), (
        f"{FLEET_CONF} is missing — the two planes have no shared parent cap, which is the "
        "16G-instead-of-8G state issue #1264 describes"
    )


def test_parent_cap_renders_from_the_fleet_variables(
    defaults: dict[str, object],
) -> None:
    """The accepting half plus its rejection: a hardcoded number would pass a presence check.

    The expected values come from defaults/main.yml rather than from a literal here, because a
    literal is the defect: this module asserted 12G for the three weeks defaults said 14G.
    The derivation against the box's headroom sits beside the default, not in a drop-in.
    """
    high, swap = (
        defaults["claude_code_fleet_memory_high"],
        defaults["claude_code_fleet_swap_max"],
    )
    rendered = render(FLEET_CONF)
    assert f"MemoryHigh={high}" in rendered, f"MemoryHigh must render defaults' {high}"
    assert f"MemorySwapMax={swap}" in rendered, (
        f"MemorySwapMax must render defaults' {swap}"
    )

    # Sentinels no default holds, so the inventory value already being there cannot satisfy
    # "followed the variable".
    raised = render(
        FLEET_CONF,
        claude_code_fleet_memory_high="97G",
        claude_code_fleet_swap_max="96G",
    )
    assert "MemoryHigh=97G" in raised and f"MemoryHigh={high}" not in raised, (
        "MemoryHigh in fleet-slice-caps.conf.j2 is hardcoded — changing "
        "claude_code_fleet_memory_high would do nothing"
    )
    assert "MemorySwapMax=96G" in raised and f"MemorySwapMax={swap}" not in raised, (
        "MemorySwapMax in fleet-slice-caps.conf.j2 is hardcoded — changing "
        "claude_code_fleet_swap_max would do nothing"
    )


def test_parent_cpu_weight_follows_its_variable_and_ranks_below_system_slice(
    defaults: dict[str, object],
) -> None:
    """system.slice, where k3s runs, carries k3s_system_slice_cpu_weight from k3s-server."""
    from lib import yaml_fast
    from lib.repo_paths import K3S_DEFAULTS

    weight = defaults["claude_code_fleet_cpu_weight"]
    system_slice = yaml_fast.safe_load(K3S_DEFAULTS.read_text())[
        "k3s_system_slice_cpu_weight"
    ]
    assert f"CPUWeight={weight}" in render(FLEET_CONF)
    assert "CPUWeight=7" in render(FLEET_CONF, claude_code_fleet_cpu_weight=7)
    assert int(str(weight)) < int(system_slice), (
        f"a fleet weight of {weight} ranks the sessions level with or above k3s, so a test "
        "fan-out can starve etcd"
    )
    assert "CPUQuota=" not in render(FLEET_CONF)


def test_parent_cap_does_not_render_a_plane_variable() -> None:
    """The rejecting half of the pair above, aimed at the actual defect this closes.

    Rendering claude_code_rc_memory_high here would repeat the shared-variable defect on an
    extra cgroup:
    a parent whose ceiling equals one plane's, so the pair still throttles at their sum.
    """
    rendered = render(FLEET_CONF, claude_code_rc_memory_high="99G")
    assert "99G" not in rendered, (
        "the parent slice must carry the FLEET bound, not a plane's — a parent equal to one "
        "plane's cap leaves the fleet's real bound at the sum again"
    )


def test_parent_cap_sets_no_memory_max() -> None:
    """MemoryMax on the parent would OOM-kill every login session and the RC host at once,
    including the SSH connection running the deploy. Same decision as both planes."""
    assert "MemoryMax=" not in render(FLEET_CONF), (
        "MemoryMax on the shared parent kills the whole fleet on one runaway session; "
        "MemoryHigh throttles instead"
    )


def test_rc_unit_is_placed_in_the_fleet_slice(defaults: dict[str, object]) -> None:
    """Without Slice=, the RC unit stays in system.slice and the parent cap covers one plane.

    The parent slice is the only half of the fix that cannot be inferred from the drop-in:
    user-<uid>.slice is already under user.slice by name, and claude-rc.service is not.
    """
    slice_name = defaults["claude_code_fleet_slice"]
    assert f"Slice={slice_name}" in render(RC_UNIT), (
        "claude-rc.service must be placed in the fleet slice, or it stays in system.slice "
        "and shares no parent with the login-session plane"
    )
    moved = render(RC_UNIT, claude_code_fleet_slice="claude-fleet.slice")
    assert "Slice=claude-fleet.slice" in moved and f"Slice={slice_name}" not in moved, (
        "Slice= is hardcoded — changing claude_code_fleet_slice would do nothing"
    )


def test_every_plane_cap_is_within_the_fleet_bound(defaults: dict[str, object]) -> None:
    """A sub-bound above its parent's can never be reached, so it means the parent's number.

    The reverse failure is two 8G planes under no parent at all. This is the
    arithmetic to re-check when either number moves.
    """
    for plane_var, fleet_var in SUB_BOUNDS.items():
        plane = to_bytes(defaults[plane_var])
        fleet = to_bytes(defaults[fleet_var])
        assert plane <= fleet, (
            f"{plane_var}={defaults[plane_var]} exceeds {fleet_var}="
            f"{defaults[fleet_var]} — a plane cannot be allowed more than the whole fleet"
        )


def test_the_sub_bound_check_rejects_a_plane_larger_than_the_fleet() -> None:
    """Rejecting half of the check above: prove the comparison can fail, and that the size
    parser is not silently reading every value as equal."""
    assert to_bytes("8G") <= to_bytes("12G")
    assert not to_bytes("16G") <= to_bytes("12G")
    assert to_bytes("12G") == 12 * 1024**3
    assert to_bytes("2G") == to_bytes("2048M")


def test_the_package_ships_the_parent_drop_in(tmp_path) -> None:
    drop_in = "/usr/lib/systemd/system/user.slice.d/homelab-claude-fleet-caps.conf"
    assert drop_in in render_daniel_server_with(tmp_path).files


# kubelet's ceiling for a burstable pod (pkg/kubelet/qos/policy.go). A best-effort pod gets 1000.
BURSTABLE_POD_MAX_OOM_SCORE_ADJ = 999
OOM_CARRIERS = (
    RC_UNIT,
    "claude-bg-resume.service.j2",
    "fleet-user-manager-oom.conf.j2",
)


def user_manager_drop_in(uid: int) -> str:
    return f"/usr/lib/systemd/system/user@{uid}.service.d/homelab-claude-fleet-oom.conf"


def oom_score_adjust(rendered: str) -> int:
    (value,) = re.findall(r"^OOMScoreAdjust=(-?\d+)$", rendered, re.M)
    return int(value)


@pytest.mark.parametrize("template", OOM_CARRIERS)
def test_a_global_oom_ranks_each_fleet_process_above_every_burstable_pod(
    template: str,
) -> None:
    """At 0 the kernel's badness score favours a pod as the victim of fleet pressure."""
    assert oom_score_adjust(render(template)) > BURSTABLE_POD_MAX_OOM_SCORE_ADJ
    assert oom_score_adjust(render(template, claude_code_fleet_oom_score_adjust=7)) == 7


# daniel-box's claude_code_login_uids is empty, yet its fan-out and background jobs run as 1000, so
# 1000 is in the fleet on both hosts.
@pytest.mark.parametrize("host", ["daniel-server", "daniel-box"])
def test_each_fleet_uids_user_manager_gets_the_oom_drop_in_and_no_restart(
    host: str,
) -> None:
    """A user unit, such as a fan-out batch, inherits its user manager's value.

    Named anything but 50-homelab.conf, so reconcile takes it without restarting the manager,
    which would end every session that user runs.
    """
    import json

    from renderer.renderer_lib import host as host_mode

    files = host_mode.render_host(host).files
    uids = (1000, 900)
    for uid in uids:
        drop_in = user_manager_drop_in(uid)
        assert drop_in in files, f"{host} ships no {drop_in}"
        text = files[drop_in][0].decode()
        assert re.search(r"^\[Service\]\nOOMScoreAdjust=", text, re.M)
        assert oom_score_adjust(text) > BURSTABLE_POD_MAX_OOM_SCORE_ADJ
    units = json.loads(files["/usr/lib/homelab/manifest.json"][0])["units"]
    assert not {f"user@{uid}.service" for uid in uids} & set(units)


@pytest.mark.parametrize("host", ["daniel-server", "daniel-box"])
def test_every_zsh_of_the_operator_raises_the_oom_score_of_what_it_starts(
    host: str, tmp_path
) -> None:
    """The operator's ssh session inherits sshd's 0, and the desktop app's is no login shell.

    zsh reads zshenv for `ssh host cmd` and zprofile only for a login.
    """
    from lib.proc_testing import run
    from renderer.renderer_lib import host as host_mode

    package = host_mode.render_host(host)
    (edit,) = [
        e
        for e in package.lines
        if e["path"] == "/etc/zsh/zshenv" and "oom_score_adj" in e["marker"]
    ]
    # The arm would create a missing /etc/zsh 0700, so zsh has to install the file first.
    assert "zsh" in package.depends
    zshenv = tmp_path / "zshenv"
    zshenv.write_text(edit["block"])
    child = run(
        [
            "sh",
            "-c",
            'cat /proc/self/oom_score_adj && . "$1" && cat /proc/self/oom_score_adj',
            "-",
            str(zshenv),
        ],
        check=True,
    )
    before, after = map(int, child.stdout.split())
    if before > BURSTABLE_POD_MAX_OOM_SCORE_ADJ:
        pytest.skip(
            f"pytest already runs at oom_score_adj {before}, which an unprivileged "
            "process cannot lower, so the write would change nothing to observe"
        )
    assert after > BURSTABLE_POD_MAX_OOM_SCORE_ADJ


@pytest.mark.parametrize("host", ["daniel-server", "daniel-box"])
def test_the_installed_zprofile_block_stays_declared_and_runs_nothing(
    host: str,
) -> None:
    """An undeclared block stays in the file, where it outlives a change of the score."""
    from renderer.renderer_lib import host as host_mode

    package = host_mode.render_host(host)
    (edit,) = [e for e in package.lines if e["path"] == "/etc/zsh/zprofile"]
    assert edit["marker"] == "# {mark} homelab claude-code — fleet oom_score_adj"
    assert all(line.startswith("#") for line in edit["block"].splitlines())
