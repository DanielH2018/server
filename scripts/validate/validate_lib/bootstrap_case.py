#!/usr/bin/env python3
"""The container cases for `hosts/bootstrap-host.sh`, against real apt, dpkg and systemd (H7.15a).

`hosts/lib/tests/test_bootstrap_host*.py` runs the script on a fake root with stub commands.
These cases run it as root in the test image, booted with systemd as PID 1 and named after the
host, so apt resolves the packages' real `Depends`, the real k3s installer writes its unit, and
systemd loads that unit:

  * `--local-build` on a container named after each host the script knows. `--tools-seed` gives
    it the pins the kit's tools-cache holds and the kit's stand-in k3s with its checksum list, so
    no case downloads the k3s release. It fetches uv's packages from the network, then builds
    the three packages from a scratch clone of the checkout.
  * `--local-build --offline <kit>` on daniel-box, in a container with no network at all, which
    is stricter than the spec's unresolvable GitHub and GHCR hostnames. The kit is stocked on the
    runner from the script's own pins. Its output says the clone is unchecked.
  * The kit's two red fixtures, each the green kit with one file changed and each in a fresh
    container: with no k3s binary the script refuses and names `k3s`, and with an nfpm archive
    that differs from its pin it refuses and names the archive.
  * The local build's red fixture (#6244): a second scratch clone, one commit past the first,
    gives homelab-daniel-box a `Depends` on HOMELAB_ONLY, which only the homelab repository
    carries. The offline build on daniel-box must then reach step 5's install, and apt's
    simulation must refuse it. Without it the green cases prove only that today's Depends
    resolve, never that the simulation can fail.
  * The fixture host (H7.15d): a third scratch clone takes a new host by the file list of
    `docs/host-packages.md`, *Adding a host* (`fixture_host_case.py`). The offline build then
    renders, builds and simulates the install of that host's package on a container of its name.

`run` prepares the clones, the kit and the seed once, then runs the cases concurrently, at
most one per CPU (#7785). Each case boots its own container and mounts its clone and seed
read-only. The script copies out of a kit, and uv runs the kit's `uv-python` interpreter in
place, taking at most a file lock there. The verdicts print in plan order. A container's own
diagnostics print as they happen, outside the group's block.

The cases read `systemctl show` after the run: the installer leaves the node's k3s unit disabled
and stopped, because the operator starts it by hand (`docs/host-packages.md`). The install is a
simulation, so a package's enable link never lands here. `hosts/lib/tests/test_bootstrap_host_k3s.py`
reads the rendered links instead.

The script runs unmodified, with four stand-ins around it:

  * The scratch clone is one commit of the checkout's tree with `hosts/lib/bootstrap_anchor.py`
    replaced by ANCHOR_STUB. The real check reads GitHub, and a pull request's head is on no
    first-parent chain of master. `hosts/lib/tests/test_bootstrap_anchor.py` holds its decision.
  * An `apt-get` on PATH ahead of the real one records each call and turns `install` into
    `install --simulate`. A real install of a cluster node's package starts reconcile, whose arms
    a container cannot satisfy, so these cases run no maintainer script.
  * A `dig` on PATH (DIG_SHIM) answers step 6's probe of daniel-box's forwarder. The simulated
    install leaves no coredns-host.service to answer it, and the offline case has no network for
    the forwarder to reach. On a real host the package installs it and reconcile starts it.
    `hosts/lib/tests/test_bootstrap_host_k3s.py` holds the probe's refusal.
  * The script runs with SUDO_UID set to the clone's owner, as `sudo` sets it, because git
    refuses a repository that another user owns unless it was reached through sudo.

The image's own homelab.sources is removed first, so no homelab repository is configured. The
path that installs from the repository has its own case, `bootstrap_repo_case.py`.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import functools
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from lib.repo_paths import REPO
from validate.host_packages import fetch_tools, tool_fetch_module
from validate.validate_lib.fixture_host_case import FIXTURE_HOST
from validate.validate_lib.host_container import CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import run_logged
from validate.validate_lib.scratch_clones import ANCHOR_ACCEPTS, HOMELAB_ONLY, red_clone
from validate.validate_lib.scratch_clones import fixture_clone, scratch_clone

SCRIPT = REPO / "hosts" / "bootstrap-host.sh"
UNCHECKED = "is unchecked; the operator who chose it is its trust root"
APT_CALLS = "/var/log/bootstrap-case-apt.calls"
APT_SHIM = f"""#!/bin/sh
# bootstrap_case.py's apt-get, ahead of the real one on PATH: it records each call and simulates
# each install.
echo "$*" >> {APT_CALLS}
if [ "$1" = install ]; then
    shift
    exec /usr/bin/apt-get install --simulate "$@"
fi
exec /usr/bin/apt-get "$@"
"""
SWAPON_SHIM = "/usr/local/sbin/swapon"
DIG_SHIM = "/usr/local/sbin/dig"
COPY = "/root/bootstrap"
TOOLS_SEED = "/srv/tools-seed"
SEED_DIR = "bootstrap-seed"
# A join address and token for the agent, which the installer writes and nothing contacts.
AGENT_ENV = "K3S_URL=https://192.0.2.10:6443 K3S_TOKEN=bootstrap-case-token"
UNITS = {"daniel-box": "k3s.service", "daniel-server": "k3s-agent.service"}
INSTALL_REFUSED = "apt-get install of the local build failed"


def script_value(script: str, name: str) -> str:
    """The value of a top-level `NAME=value` line of the script, unquoted."""
    match = re.search(rf"^{name}=(.*)$", script, re.MULTILINE)
    if not match:
        raise CaseFailed(f"bootstrap-host.sh sets no {name}")
    return match[1].strip('"')


def kit_pins(script: str) -> dict[str, tuple[str, str]]:
    """Each amd64 kit file the script checks against a pin, by file name: (url, sha256)."""
    pins = {
        "install.sh": (
            script_value(script, "K3S_INSTALL_URL"),
            script_value(script, "K3S_INSTALL_SHA256"),
        )
    }
    for tool in ("nfpm", "uv"):
        block = re.search(rf"^  {tool}:\n    amd64:\n((?:      .*\n)+)", script, re.M)
        if not block:
            raise CaseFailed(f"bootstrap-host.sh's PINS holds no amd64 {tool}")
        fields = dict(re.findall(r"^      (\w+): (\S+)$", block[1], re.MULTILINE))
        version = fields["version"]
        url = fields["url"].replace("{version}", version)
        url = url.replace("{bare}", version.removeprefix("v"))
        pins[url.rsplit("/", 1)[1]] = (url, fields["sha256"])
    return pins


def split_log2ram(host: str, calls: str) -> tuple[str, list[str]]:
    """Take step 5's Azlux update and log2ram install out of the shim's log.

    The Pi makes those two calls after its packages, so a check of the package install reads
    the rest. The simulation installs nothing, so no Azlux source exists for the install to read.

    Args:
        host: The host the script ran for.
        calls: The shim's log, one apt-get argument list per line.

    Returns:
        The log without those calls, and a problem when the count is not two on the Pi and none
        on any other host.
    """
    lines = calls.splitlines()
    rest = [
        line
        for line in lines
        if "azlux.sources" not in line and not line.endswith(" log2ram")
    ]
    found = len(lines) - len(rest)
    want = 2 if host == "daniel-pi" else 0
    problems = (
        [] if found == want else [f"{host} ran {found} log2ram calls, not {want}"]
    )
    return "\n".join(rest), problems


def apt_problems(host: str, calls: str, stdout: str) -> list[str]:
    """What is wrong with step 5's install, from the shim's call log and the script's output.

    Args:
        host: The host the script ran for.
        calls: The shim's log, one apt-get argument list per line.
        stdout: The script's standard output, which holds apt's simulation.
    """
    want = ["homelab-tools", "homelab-updater", f"homelab-{host}"]
    package_calls, log2ram_problems = split_log2ram(host, calls)
    installs = [
        line.split()
        for line in package_calls.splitlines()
        if line.startswith("install")
    ]
    if len(installs) != 1:
        return [f"apt-get ran {len(installs)} installs, not one: {installs}"]
    debs = [arg for arg in installs[0] if arg.endswith(".deb")]
    named = [deb.rsplit("/", 1)[-1].split("_", 1)[0] for deb in debs]
    problems = [] if named == want else [f"the install named {named}, not {want}"]
    problems += [
        f"apt's simulation installs no {package}"
        for package in want
        if not re.search(rf"^Inst {re.escape(package)} ", stdout, re.MULTILINE)
    ]
    return problems + log2ram_problems


def red_kit(green: Path, out: Path, name: str, data: bytes | None) -> Path:
    """The green kit with `name` replaced by `data`, or left out when `data` is None.

    Every other entry is a symlink into the green kit, which the containers mount at its own
    path, so a red kit costs no copy of uv's cache.
    """
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for entry in green.iterdir():
        if entry.name != name:
            (out / entry.name).symlink_to(entry)
    if data is not None:
        (out / name).write_bytes(data)
    return out


def _download(
    url: str, sha256: str, dest: Path, cache: Path, opener=None, sleep=time.sleep
) -> None:
    """Copy the file `url` names to `dest`, through the tool fetch's retry window.

    Raises:
        CaseFailed: The download stayed unavailable, or its digest differs from `sha256`.
    """
    tool_fetch = tool_fetch_module()
    pin = {"url": url, "sha256": sha256}
    try:
        path = tool_fetch.download_tool(pin, cache, opener, sleep)
    except tool_fetch.Failure as exc:
        raise CaseFailed(str(exc)) from exc
    shutil.copyfile(path, dest)


def install_apt_shim(c: Container) -> None:
    """Put APT_SHIM ahead of the real apt-get, and stand-in swapon and dig commands on PATH.

    The image's overlay filesystem holds no swap file, so a real swapon fails in every case. The
    dig stand-in answers with a TEST-NET address, because the simulated install starts no forwarder.
    """
    c.ok(f"cat > /usr/local/sbin/apt-get << 'EOF'\n{APT_SHIM}EOF\n")
    c.ok(f"printf '#!/bin/sh\\nexit 0\\n' > {SWAPON_SHIM}")
    c.ok(f"printf '#!/bin/sh\\necho 192.0.2.1\\n' > {DIG_SHIM}")
    c.ok(f"chmod 0755 /usr/local/sbin/apt-get {SWAPON_SHIM} {DIG_SHIM}")


def seed_kit(cases, kit: Path) -> Path:
    """Copy the job's tools cache into the kit, and return where the kit's downloads cache.

    A pin the build job fetched then costs no request. Each use checks the file against its pin
    again, so a bad entry is fetched anew rather than installed.
    """
    if cases.tools_cache is None:
        return cases.work / "bootstrap-kit-downloads"
    shutil.copytree(cases.tools_cache, kit / "tools-cache")
    return cases.tools_cache


def stock_pins(cases, script: str, kit: Path, download=_download) -> None:
    """Put each file the script pins into the kit, reading the job's tools cache first."""
    downloads = seed_kit(cases, kit)
    for name, (url, sha256) in kit_pins(script).items():
        download(url, sha256, kit / name, downloads)


def stock_kit(cases, clone: Path, kit: Path) -> Path:
    """The break-glass kit for amd64, as `docs/break-glass.md`, item 8, lists it."""
    shutil.rmtree(kit, ignore_errors=True)
    kit.mkdir(parents=True)
    script = SCRIPT.read_text()
    stock_pins(cases, script, kit)
    # The script checks k3s against the kit's own checksum list, so a stand-in that answers
    # `--version` at the pin stands for the 70 MB binary.
    k3s = f'#!/bin/sh\necho "k3s version {script_value(script, "K3S_VERSION")}"\n'
    (kit / "k3s").write_text(k3s)
    (kit / "k3s").chmod(0o755)
    digest = hashlib.sha256(k3s.encode()).hexdigest()
    (kit / "sha256sum-amd64.txt").write_text(f"{digest}  k3s\n")
    # The clone's own render, so the cache holds exactly the pins the script's render reads.
    tools = cases.work / "bootstrap-render"
    shutil.rmtree(tools, ignore_errors=True)
    run_logged(
        [sys.executable, "scripts/validate/host_packages.py", "render"]
        + ["--tree", ".", "--out", str(tools)],
        cwd=clone,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    problems = fetch_tools(tools, kit / "tools-cache")
    expect(not problems, "; ".join(problems))
    # uv's cache and interpreter, filled by the pinned uv the script itself runs.
    uv_name = next(name for name in kit_pins(script) if name.startswith("uv-"))
    with tarfile.open(kit / uv_name) as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("/uv"))
        archive.extract(member, cases.work / "bootstrap-uv", filter="data")
    stock_uv_cache(cases, clone, kit, str(cases.work / "bootstrap-uv" / member.name))
    return kit


def stock_uv_cache(cases, clone: Path, kit: Path, uv: str, run=run_logged) -> None:
    """Fill the kit's uv cache and interpreter with what the script's `uv run --offline` reads.

    The sync skips the lint group, as the script's own `uv run` does, so no lint tool's GitHub
    release download sits in the bootstrap cases.
    """
    found = re.search(
        r'^host_python_version: "(.*)"$',
        (clone / "ansible/inventory/group_vars/all.yml").read_text(),
        re.MULTILINE,
    )
    expect(
        found is not None, "the clone's group_vars/all.yml sets no host_python_version"
    )
    python = found[1] if found else ""
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    env.pop("VIRTUAL_ENV", None)
    # A venv left from an earlier run would satisfy the sync with nothing written to the cache.
    shutil.rmtree(cases.work / "bootstrap-venv", ignore_errors=True)
    env |= {
        "UV_CACHE_DIR": str(kit / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(kit / "uv-python"),
        "UV_PYTHON_BIN_DIR": str(cases.work / "bootstrap-uv-bin"),
        "UV_PROJECT_ENVIRONMENT": str(cases.work / "bootstrap-venv"),
    }
    run([uv, "python", "install", python], env=env, check=True)
    run(
        [
            uv,
            "sync",
            "--frozen",
            "--no-group",
            "lint",
            "--python",
            python,
            "--no-python-downloads",
        ],
        cwd=clone,
        env=env,
        check=True,
    )


def stock_seed(kit: Path, seed: Path) -> Path:
    """The `--tools-seed` directory: the kit's tools cache, and its k3s with its checksum list.

    With the k3s pair seeded, a cluster node's online run places the stand-in rather than
    downloading the release binary, so a GitHub release outage cannot fail the case.
    """
    shutil.rmtree(seed, ignore_errors=True)
    shutil.copytree(kit / "tools-cache", seed)
    for name in ("k3s", "sha256sum-amd64.txt"):
        shutil.copy2(kit / name, seed / name)
    return seed


class Bootstrap:
    """One run of the script in a fresh container named after its host."""

    def __init__(
        self,
        cases,
        host: str,
        clone: Path,
        kit: Path | None,
        label: str,
        seed: Path | None = None,
    ):
        mounts = (f"{clone}:/srv/clone:ro",)
        if seed is not None:
            mounts += (f"{seed}:{TOOLS_SEED}:ro",)
        if kit is not None:
            # Every kit at its own path: a red kit links into the green one, and uv-python's
            # interpreter links name absolute paths.
            kits = kit.parent
            mounts += (f"{kits}:{kits}",)
        self.cases, self.host, self.clone, self.kit = cases, host, clone, kit
        self.seed = seed
        self.container = Container(
            cases.image,
            f"homelab-host-test-bootstrap-{label}",
            cases.work,
            hostname=host,
            mounts=mounts,
            network="none" if kit is not None else None,
        )

    def run(self) -> subprocess.CompletedProcess:
        c = self.container
        c.boot(self.cases.python)
        c.ok("rm -f /etc/apt/sources.list.d/homelab.sources")
        install_apt_shim(c)
        c.ok(
            f"install -d {COPY} && cp /srv/clone/hosts/bootstrap-host.sh"
            f" /srv/clone/hosts/keys/homelab-archive-keyring.gpg {COPY}/"
        )
        env = f"SUDO_UID={self.clone.stat().st_uid}"
        if self.host == "daniel-server":
            env += f" {AGENT_ENV}"
        flags = "--local-build /srv/clone"
        if self.kit is not None:
            flags += f" --offline {self.kit}"
        if self.seed is not None:
            flags += f" --tools-seed {TOOLS_SEED}"
        return c.sh(f"{env} sh {COPY}/bootstrap-host.sh {self.host} {flags}", 1800)

    def unit_problems(self) -> list[str]:
        """What is wrong with the k3s units the installer left, as systemd loads them.

        The node's own unit must also be disabled and inactive, because k3s's first start belongs
        to the operator, after the restores.
        """
        c = self.container
        c.ok("systemctl daemon-reload")
        problems = []
        for host, unit in UNITS.items():
            state = c.ok(f"systemctl show -P LoadState {unit}").strip()
            want = "loaded" if host == self.host else "not-found"
            if state != want:
                problems.append(f"{unit} is {state}, not {want}")
            if host == self.host:
                for prop, stopped in (
                    ("UnitFileState", "disabled"),
                    ("ActiveState", "inactive"),
                ):
                    value = c.ok(f"systemctl show -P {prop} {unit}").strip()
                    if value != stopped:
                        problems.append(f"{unit}'s {prop} is {value}, not {stopped}")
        return problems

    def baseline_problems(self) -> list[str]:
        """What step 4 left wrong, read from a clean image: sshd admits only `ssh-users`."""
        c = self.container
        problems = []
        if "ssh-users" not in c.ok("id -nG ubuntu").split():
            problems.append("ubuntu is not in the ssh-users group after step 4")
        if (
            c.ok("systemctl is-enabled networkd-dispatcher.service || true").strip()
            != "masked"
        ):
            problems.append("networkd-dispatcher.service is not masked after step 4")
        if self.host == "daniel-pi" and "/swapfile none swap sw 0 0" not in c.read(
            "/etc/fstab"
        ):
            problems.append("/etc/fstab holds no swap line after step 4")
        return problems


def _verdict(body) -> Exception | None:
    """Run `body`, and return the failure it raised, or None when it passed."""
    try:
        body()
    except (CaseFailed, OSError, subprocess.SubprocessError) as exc:
        return exc


def output_problems(
    returncode: int, stdout: str, stderr: str, marker: str, absent: str | None
) -> list[str]:
    """What is wrong with a green run's exit status and output.

    Args:
        returncode: The script's exit status.
        stdout: Its standard output.
        stderr: Its standard error.
        marker: A line the run must print.
        absent: A line the run must not print, or None.
    """
    tail = (stdout + stderr)[-3000:]
    problems = [] if returncode == 0 else [f"the script exited {returncode}:\n{tail}"]
    if marker not in stdout:
        problems.append(f"the output holds no {marker!r}:\n{tail}")
    if absent is not None and absent in stdout + stderr:
        problems.append(f"the output holds {absent!r}")
    if "bootstrap-case-token" in stdout + stderr:
        problems.append("the output holds the agent's token")
    return problems


def _expect_green(run: Bootstrap, marker: str, absent: str | None = None) -> None:
    try:
        result = run.run()
        problems = output_problems(
            result.returncode, result.stdout, result.stderr, marker, absent
        )
        expect(not problems, "; ".join(problems))
        calls = run.container.read(APT_CALLS)
        problems = (
            apt_problems(run.host, calls, result.stdout)
            + run.unit_problems()
            + run.baseline_problems()
        )
        expect(not problems, "; ".join(problems))
    finally:
        run.container.remove()


def _expect_refused(run: Bootstrap, reason: str, apt_says: str | None) -> None:
    """Expect the script to exit 1 naming `reason`.

    With `apt_says` None the refusal comes before step 5, so apt-get ran no install. Otherwise
    apt-get ran one install, and apt's output must hold `apt_says`, which tells the refusal
    apart from an install that failed for another reason.
    """
    try:
        result = run.run()
        expect(
            result.returncode == 1 and reason in result.stderr,
            f"the script exited {result.returncode}, not 1 naming {reason!r}:"
            f"\n{result.stderr[-3000:]}",
        )
        installs = [
            line
            for line in run.container.read(APT_CALLS).splitlines()
            if line.startswith("install")
        ]
        if apt_says is None:
            expect(not installs, f"the refused run still installed: {installs}")
        else:
            expect(len(installs) == 1, f"apt-get ran {len(installs)} installs, not one")
            output = result.stdout + result.stderr
            expect(
                apt_says in output,
                f"apt's output holds no {apt_says!r}:\n{output[-3000:]}",
            )
    finally:
        run.container.remove()


class Planned(NamedTuple):
    """One bootstrap case: the host and kit it runs with, and the verdict it expects.

    Attributes:
        name: The case's name, as the job log prints it.
        label: The container's label.
        host: The host the script runs for.
        kit: The offline kit, or None for an online run.
        marker: A line a green run must print; None for a red one.
        refusal: The refusal a red run must print; None for a green one.
        clone: Which scratch clone the run builds: "green", "red-depends" for `red_clone`'s, or
            "fixture-host" for `fixture_clone`'s.
        apt_says: What apt must print when a red run's refusal is apt's; None when the
            refusal comes before any install.
        seed: The directory `--tools-seed` reads, or None for an offline run, which has the kit.
    """

    name: str
    label: str
    host: str
    kit: Path | None
    marker: str | None
    refusal: str | None
    clone: str = "green"
    apt_says: str | None = None
    seed: Path | None = None


def plan(work: Path, green: Path) -> list[Planned]:
    """Every bootstrap case, in order, writing each red kit beside the green one."""
    script = SCRIPT.read_text()
    # DECIDED: every online case seeds k3s, so no case exercises install.sh's own binary
    # download or its check of that binary. One unseeded case fails the run whenever a GitHub
    # release outage outlasts `retry`'s window (#7625, #7729), and install.sh still writes the
    # unit that unit_problems reads. #7754 tracks a non-blocking online leg.
    cases = [
        Planned(
            f"bootstrap {host} with --local-build",
            host,
            host,
            None,
            ANCHOR_ACCEPTS,
            None,
            seed=work / SEED_DIR,
        )
        for host in script_value(script, "HOSTS").split()
    ]
    cases.append(
        Planned(
            "bootstrap daniel-box offline from the kit",
            "offline",
            "daniel-box",
            green,
            UNCHECKED,
            None,
        )
    )
    nfpm = next(name for name in kit_pins(script) if name.startswith("nfpm"))
    reds = (
        ("k3s", None, "the offline kit has no k3s"),
        (nfpm, b"not the pinned nfpm", f"{nfpm} does not match its pinned sha256"),
    )
    for name, data, reason in reds:
        kit = red_kit(green, work / "bootstrap-kit" / f"red-{name}", name, data)
        label = f"red-{name.split('_')[0]}"
        # The name says which fixture failed: a dropped file and a changed one are different reds.
        defect = "is missing" if data is None else "does not match its pin"
        cases.append(
            Planned(
                f"refuse a kit whose {name} {defect}",
                label,
                "daniel-box",
                kit,
                None,
                reason,
            )
        )
    # Offline, so the red needs no network and differs from the green offline case in its
    # clone alone.
    cases.append(
        Planned(
            f"refuse a local build whose homelab-daniel-box Depends on {HOMELAB_ONLY}",
            "red-depends",
            "daniel-box",
            green,
            None,
            INSTALL_REFUSED,
            "red-depends",
            f"Depends: {HOMELAB_ONLY}",
        )
    )
    # Offline, like the other cases that differ from the green build in their clone alone.
    cases.append(
        Planned(
            f"bootstrap {FIXTURE_HOST}, made from the Adding a host walkthrough",
            "fixture-host",
            FIXTURE_HOST,
            green,
            UNCHECKED,
            None,
            "fixture-host",
        )
    )
    return cases


def bootstrap_for(cases, item: Planned, clones: dict[str, Path]) -> Bootstrap:
    """The run `item` plans."""
    return Bootstrap(
        cases, item.host, clones[item.clone], item.kit, item.label, item.seed
    )


def body_for(cases, item: Planned, clones: dict[str, Path]) -> Callable[[], None]:
    """The run `item` plans, as a call that raises CaseFailed when its verdict fails."""
    bootstrap = bootstrap_for(cases, item, clones)
    if item.refusal is not None:
        return functools.partial(
            _expect_refused, bootstrap, item.refusal, item.apt_says
        )
    # Offline the script makes neither GitHub read, so the anchor stub must not run.
    absent = ANCHOR_ACCEPTS if item.kit is not None else None
    return functools.partial(_expect_green, bootstrap, item.marker or "", absent)


def run(cases) -> None:
    """Run every bootstrap case, and record each verdict on `cases`."""
    clone = cases.work / "bootstrap-clone"
    green = cases.work / "bootstrap-kit" / "green"
    try:
        scratch_clone(clone)
        clones = {
            "green": clone,
            "red-depends": red_clone(clone, cases.work / "bootstrap-clone-red-depends"),
            "fixture-host": fixture_clone(
                clone, cases.work / "bootstrap-clone-fixture"
            ),
        }
        stock_kit(cases, clone, green)
        stock_seed(green, cases.work / SEED_DIR)
        planned = plan(cases.work, green)
    except (CaseFailed, OSError, subprocess.SubprocessError, StopIteration) as exc:
        cases.failed.append("prepare the bootstrap cases")
        print(f"[FAIL] prepare the bootstrap cases: {exc}", file=sys.stderr)
        return
    bodies = [body_for(cases, item, clones) for item in planned]
    # Printed from this thread, in plan order: case_groups buffers stderr per group thread.
    # The offline items are CPU-bound builds, and other case groups share the runner's CPUs.
    workers = min(len(bodies), max(2, os.cpu_count() or 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item, failure in zip(planned, pool.map(_verdict, bodies), strict=True):
            if failure is None:
                print(f"[PASS] {item.label}: {item.name}", file=sys.stderr)
            else:
                cases.failed.append(item.name)
                print(f"[FAIL] {item.label}: {item.name}: {failure}", file=sys.stderr)
