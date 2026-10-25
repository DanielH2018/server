#!/usr/bin/env python3
"""The container cases for `hosts/bootstrap-host.sh`, against real apt, dpkg and systemd (H7.15a).

`hosts/lib/tests/test_bootstrap_host*.py` runs the script on a fake root with stub commands.
These cases run it as root in the test image, booted with systemd as PID 1 and named after the
host, so apt resolves the packages' real `Depends`, the real k3s installer writes its unit, and
systemd loads that unit:

  * `--local-build` on a container named after each host the script knows. It fetches nfpm, uv,
    uv's packages and, on a cluster node, k3s from the network, then builds the three packages
    from a scratch clone of the checkout.
  * `--local-build --offline <kit>` on daniel-box, in a container with no network at all, which
    is stricter than the spec's unresolvable GitHub and GHCR hostnames. The kit is stocked on the
    runner from the script's own pins. Its output says the clone is unchecked.
  * The kit's two red fixtures, each the green kit with one file changed and each in a fresh
    container: with no k3s binary the script refuses and names `k3s`, and with an nfpm archive
    that differs from its pin it refuses and names the archive.

The script runs unmodified, with three stand-ins around it:

  * The scratch clone is one commit of the checkout's tree with `hosts/lib/bootstrap_anchor.py`
    replaced by ANCHOR_STUB. The real check reads GitHub, and a pull request's head is on no
    first-parent chain of master. `hosts/lib/tests/test_bootstrap_anchor.py` holds its decision.
  * An `apt-get` on PATH ahead of the real one records each call and turns `install` into
    `install --simulate`. A real install of a cluster node's package starts reconcile, whose arms
    a container cannot satisfy, so these cases run no maintainer script.
  * The script runs with SUDO_UID set to the clone's owner, as `sudo` sets it, because git
    refuses a repository that another user owns unless it was reached through sudo.

The image's own homelab.sources is removed first, so no homelab repository is configured. These
cases do not cover the path that installs from the repository, because its archive key's private
half lives only on daniel-box; H7.44's guest drill owns that proof.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from lib.git import git, git_process
from lib.repo_paths import REPO
from validate.host_packages import fetch_tools
from validate.validate_lib.host_container import CaseFailed, Container, expect

SCRIPT = REPO / "hosts" / "bootstrap-host.sh"
ANCHOR = "hosts/lib/bootstrap_anchor.py"
ANCHOR_ACCEPTS = "bootstrap-host: the case's stub anchor accepts"
ANCHOR_STUB = f'''"""bootstrap_case.py's stand-in for bootstrap_anchor.py in the case's scratch clone."""

import sys

print(f"{ANCHOR_ACCEPTS} {{sys.argv[2][:12]}}")
'''
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
COPY = "/root/bootstrap"
# A join address and token for the agent, which the installer writes and nothing contacts.
AGENT_ENV = "K3S_URL=https://192.0.2.10:6443 K3S_TOKEN=bootstrap-case-token"
UNITS = {"daniel-box": "k3s.service", "daniel-server": "k3s-agent.service"}


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


def apt_problems(host: str, calls: str, stdout: str) -> list[str]:
    """What is wrong with step 5's install, from the shim's call log and the script's output.

    Args:
        host: The host the script ran for.
        calls: The shim's log, one apt-get argument list per line.
        stdout: The script's standard output, which holds apt's simulation.
    """
    want = ["homelab-tools", "homelab-updater", f"homelab-{host}"]
    installs = [
        line.split() for line in calls.splitlines() if line.startswith("install")
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
    return problems


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


def _download(url: str, sha256: str, dest: Path) -> None:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                dest.write_bytes(response.read())
            break
        except OSError:
            if attempt == 2:
                raise
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    expect(digest == sha256, f"{url} has sha256 {digest}, not its pin {sha256}")


def _git(*argv: str, cwd: Path) -> None:
    identity = [
        "-c",
        "user.name=bootstrap-case",
        "-c",
        "user.email=case@example.invalid",
    ]
    git(*identity, "-c", "commit.gpgsign=false", *argv, cwd=cwd)


def scratch_clone(out: Path) -> Path:
    """A one-commit git repository holding the checkout's HEAD tree and ANCHOR_STUB."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    archive = git_process("archive", "HEAD", cwd=REPO)
    assert archive.stdin and archive.stdout
    archive.stdin.close()
    subprocess.run(["tar", "-x", "-C", str(out)], stdin=archive.stdout, check=True)
    archive.stdout.close()
    expect(archive.wait() == 0, f"git archive of {REPO} exited {archive.returncode}")
    (out / ANCHOR).write_text(ANCHOR_STUB)
    _git("init", "-q", cwd=out)
    _git("add", "-A", "-f", cwd=out)
    _git("commit", "-q", "-m", "bootstrap case: stub the anchor check", cwd=out)
    return out


def stock_kit(cases, clone: Path, kit: Path) -> Path:
    """The break-glass kit for amd64, as `docs/break-glass.md`, item 8, lists it."""
    shutil.rmtree(kit, ignore_errors=True)
    kit.mkdir(parents=True)
    script = SCRIPT.read_text()
    for name, (url, sha256) in kit_pins(script).items():
        _download(url, sha256, kit / name)
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
    subprocess.run(
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
    uv = str(cases.work / "bootstrap-uv" / member.name)
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
    subprocess.run([uv, "python", "install", python], env=env, check=True)
    subprocess.run(
        [uv, "sync", "--frozen", "--python", python, "--no-python-downloads"],
        cwd=clone,
        env=env,
        check=True,
    )
    return kit


class Bootstrap:
    """One run of the script in a fresh container named after its host."""

    def __init__(self, cases, host: str, clone: Path, kit: Path | None, label: str):
        mounts = (f"{clone}:/srv/clone:ro",)
        if kit is not None:
            # Every kit at its own path: a red kit links into the green one, and uv-python's
            # interpreter links name absolute paths.
            kits = kit.parent
            mounts += (f"{kits}:{kits}",)
        self.cases, self.host, self.clone, self.kit = cases, host, clone, kit
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
        c.ok(f"cat > /usr/local/sbin/apt-get << 'EOF'\n{APT_SHIM}EOF\n")
        c.ok("chmod 0755 /usr/local/sbin/apt-get")
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
        return c.sh(f"{env} sh {COPY}/bootstrap-host.sh {self.host} {flags}", 1800)

    def unit_problems(self) -> list[str]:
        """What is wrong with the k3s units the installer left, as systemd loads them."""
        c = self.container
        c.ok("systemctl daemon-reload")
        problems = []
        for host, unit in UNITS.items():
            state = c.ok(f"systemctl show -P LoadState {unit}").strip()
            want = "loaded" if host == self.host else "not-found"
            if state != want:
                problems.append(f"{unit} is {state}, not {want}")
        return problems


def _report(cases, name: str, label: str, body) -> None:
    try:
        body()
    except (CaseFailed, OSError, subprocess.SubprocessError) as exc:
        cases.failed.append(name)
        print(f"[FAIL] {label}: {name}: {exc}", file=sys.stderr)
    else:
        print(f"[PASS] {label}: {name}", file=sys.stderr)


def _expect_green(run: Bootstrap, marker: str) -> None:
    try:
        result = run.run()
        out = result.stdout + result.stderr
        expect(
            result.returncode == 0,
            f"the script exited {result.returncode}:\n{out[-3000:]}",
        )
        expect(
            marker in result.stdout, f"the output holds no {marker!r}:\n{out[-3000:]}"
        )
        expect("bootstrap-case-token" not in out, "the output holds the agent's token")
        calls = run.container.read(APT_CALLS)
        problems = apt_problems(run.host, calls, result.stdout) + run.unit_problems()
        expect(not problems, "; ".join(problems))
    finally:
        run.container.remove()


def _expect_refused(run: Bootstrap, reason: str) -> None:
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
        expect(not installs, f"the refused run still installed: {installs}")
    finally:
        run.container.remove()


def run(cases) -> None:
    """Run every bootstrap case, and record each verdict on `cases`."""
    clone = cases.work / "bootstrap-clone"
    green = cases.work / "bootstrap-kit" / "green"
    try:
        scratch_clone(clone)
        stock_kit(cases, clone, green)
    except (CaseFailed, OSError, subprocess.SubprocessError, StopIteration) as exc:
        cases.failed.append("prepare the bootstrap cases")
        print(f"[FAIL] prepare the bootstrap cases: {exc}", file=sys.stderr)
        return
    for host in script_value(SCRIPT.read_text(), "HOSTS").split():
        online = Bootstrap(cases, host, clone, None, host)
        _report(
            cases,
            f"bootstrap {host} with --local-build",
            host,
            lambda r=online: _expect_green(r, ANCHOR_ACCEPTS),
        )
    offline = Bootstrap(cases, "daniel-box", clone, green, "offline")
    _report(
        cases,
        "bootstrap daniel-box offline from the kit",
        "offline",
        lambda: _expect_green(offline, UNCHECKED),
    )
    nfpm = next(
        name for name in kit_pins(SCRIPT.read_text()) if name.startswith("nfpm")
    )
    reds = (
        ("k3s", None, "the offline kit has no k3s"),
        (nfpm, b"not the pinned nfpm", f"{nfpm} does not match its pinned sha256"),
    )
    for name, data, reason in reds:
        kit = red_kit(green, cases.work / "bootstrap-kit" / f"red-{name}", name, data)
        red = Bootstrap(cases, "daniel-box", clone, kit, f"red-{name.split('_')[0]}")
        _report(
            cases,
            f"refuse a kit whose {name} is wrong",
            f"red-{name}",
            lambda r=red, why=reason: _expect_refused(r, why),
        )
