#!/usr/bin/env python3
"""Render every systemd unit under ansible/roles/ and hosts/, and verify the output.

`shell_templates.py` covers `*.sh.j2` and `k8s_manifests.py` covers `roles/k8s/*/templates/*.j2`;
`*.service.j2` / `*.timer.j2` fell between them — nothing rendered a unit template or checked the
result, so a typo'd directive key reached a live host with `daemon_reload: true` reporting
success and systemd loading the unit anyway, ignoring the bad line with only a journal warning
(GitHub issue #948).

Render context = `lib.render_context`: StubUndefined (`scripts.lib.render_guard`) under the
OWNING ROLE's `defaults/main.yml` under `ansible/inventory/group_vars/all.yml`, every value
expanded the way Ansible expands it. Bare stubs are not enough: `systemd-analyze verify` treats
`OnCalendar=STUB` and `OnUnitActiveSec=STUB` as parse failures, so 3 of the 17 live timers
(gitops-deploy, renovate-agent, claude-rc-restart) were red on a clean repo before their role's
real schedule value was layered in. What still renders as STUB: `inventory_hostname` and
`ansible_managed` (Ansible magic vars with no plaintext fallback here — both are used only in
comments or human-facing alert text, never in a directive systemd parses). Until #3692 a default
whose own value referenced another variable (`renovate_agent_repo_dir: "/home/{{ sys_user
}}/server"`) reached the unit as literal braces; it now arrives as the resolved path.

`systemd-analyze verify`'s EXIT STATUS is ignored — measured on systemd 255.4, it is 0 on
`SuccessExitStatuss=75` (a typo'd key) and on `TimeoutStartSec=6zz0min` (an unparsable value) on
at least one host, even though both print a diagnostic line. The only reliable signal is stderr:
a line attributed to the rendered unit's own path (`<tmp>/<unit>:<line>: ...`) matching
`Unknown key|Failed to parse|Assignment outside of section` — `Unknown key`, not the
`Unknown key name` systemd 255 prints, because v257 reworded that one message and the narrower
pattern silently matched nothing on the systemd 259 the ubuntu-26.04 runner carries.
`--man=no --recursive-errors=no`
drops `k3s.service: Failed to open ... Permission denied` noise from a followed `After=`/`Wants=`
target that systemd-analyze cannot read outside its normal unit search path, and a bare
`Command ... is not executable` (no `path:line:` prefix, so it never matches the attribution
check) is deliberately not a failure — this hook proves the unit PARSES, not that its ExecStart
binary exists on the render host.

Structural check only, same scope as `shell_templates.py`: this catches a typo'd DIRECTIVE KEY,
not a wrong value (`TimeoutStartSec=45min` when the intent was 60) or a misspelled `OnFailure=`
TARGET — `--recursive-errors=no` suppresses the line that would name a target unit systemd can't
find.

The host packages under `hosts/` render through the host renderer
(`scripts/renderer/renderer_lib/host.py`), not a `.j2` glob, because a package unit may be a
static file the glob would miss. Every unit the renderer emits is verified with its drop-ins
laid out beside it: each host package installs with every shared package, so a host's drop-in
over the shared `homelab-updater.timer` is verified over that timer. A component no host
package lists yet renders through a package of its own. The run fails unless it verified
`homelab-updater.timer`, so a host render that emits nothing cannot pass (H3.2c,
`docs/target-architecture/host/h3-pi-pilot.md`).

Also renders every polkit rule (`*.rules.j2`) under ansible/roles/ — JavaScript that the
roles' tests content-check but whose syntax nothing else checks — and runs `node --check` on
each output. A missing `node` SKIPS with a message rather than failing closed, unlike a missing
`systemd-analyze`: `systemd-analyze` is this repo's only way to verify a unit file, so its
absence must not read as "checked"; the polkit rules are checked by a tool GitHub's own runner
images always carry (they run Actions itself on Node.js), so the skip path is a
local-workstation fallback, not a gate this repo depends on being armed in CI.

Run directly or via the ``validate-unit-templates`` prek hook. Exits non-zero if any unit fails
to render, if a host package fails to render or emits no `homelab-updater.timer`, if `systemd-analyze` reports a matching diagnostic, if a polkit rule fails to
render, or if `node --check` flags one. Exits non-zero if `systemd-analyze` itself isn't
available on PATH (fail loud, matching `shell_templates.py`'s policy — a missing verifier must
not silently degrade to "renders, so it's fine").
"""

import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
import tempfile
from pathlib import Path, PurePosixPath

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from validate.validate_lib.ok_lines import ok_printer
from lib.ansible_jinja_env import template_env
from lib.render_context import render_context
from renderer.renderer_lib import host as host_mode
from lib.render_guard import (
    ANSIBLE,
    REPO,
    dump_numbered,
    load_yaml,
    render_or_error,
)

ROLES = ANSIBLE / "roles"
HOSTS = host_mode.HOSTS

# A line systemd-analyze attributes to the file it's checking looks like
# "<path>:<lineno>: <message>". A line about a followed unit ("k3s.service: Failed to open...")
# or an ExecStart binary check ("notreal.service: Command ... is not executable") carries no
# ":<lineno>:" — that shape difference is what keeps this from flagging either.
#
# `Unknown key` matches both wordings systemd has used for a typo'd directive key. systemd
# v257 dropped the ` name` and squared the section brackets
# (src/shared/conf-parser.c: "Unknown key name '%s' in section '%s', ignoring." →
# "Unknown key '%s' in section [%s], ignoring."), so the old `Unknown key name` alternative
# matches nothing on systemd 259 — the version the ubuntu-26.04 runner image carries — and
# this check would go vacuous for the exact class it exists to catch.
# The other two alternatives are unchanged from v255 through v259.
_FAIL_MESSAGE = re.compile(r"Unknown key|Failed to parse|Assignment outside of section")


def discover_templates() -> list[Path]:
    """Return every *.service.j2 / *.timer.j2 / *.socket.j2 under ansible/roles/."""
    return sorted(
        [
            *ROLES.rglob("*.service.j2"),
            *ROLES.rglob("*.timer.j2"),
            *ROLES.rglob("*.socket.j2"),
        ]
    )


def discover_rules() -> list[Path]:
    """Return every polkit rule template (*.rules.j2) under ansible/roles/."""
    return sorted(ROLES.rglob("*.rules.j2"))


# Values a CALLER passes to a shared template, keyed by template basename. `roles/setup/common`
# is never run as a role, so it has no defaults to layer in, and its unit pair takes every
# directive value from the importing role's `vars:` (`tasks/kuma_check_timer.yml`). A bare
# STUB in `OnCalendar=` is a parse failure, so the pair renders with one caller's shape here.
CALLER_CONTEXT: dict[str, dict] = {
    "kuma-check.service.j2": {
        "kuma_check_name": "example",
        "kuma_check_description": "Example Kuma check",
        "kuma_check_exec": "/usr/local/bin/example.sh",
        "kuma_check_user": "root",
        "kuma_check_restart_sec": "15min",
    },
    "kuma-check.timer.j2": {
        "kuma_check_name": "example",
        "kuma_check_description": "Example Kuma check",
        "kuma_check_on_calendar": "*-*-* *:23:00",
    },
    # `tasks/alert_unit.yml`. A STUB env dir renders a relative EnvironmentFile= path.
    "unit-failure-alert.service.j2": {
        "alert_unit_name": "example",
        "alert_unit_description": "Alert when example fails",
        "alert_unit_message": "example unit failed",
        "alert_unit_env_dir": "/etc/example",
    },
}


def unit_context(template: Path) -> dict:
    """The context `template` renders with: `lib.render_context`, plus any `CALLER_CONTEXT`.

    A shared template with no owning defaults takes its caller-passed values from
    `CALLER_CONTEXT` instead.
    """
    return render_context(template, overrides=CALLER_CONTEXT.get(template.name, {}))


def systemd_verify(unit_path: Path, systemd_analyze_bin: str) -> str | None:
    """Run `systemd-analyze verify` on the rendered unit; return an error string, or None.

    The exit status is ignored by design — see the module docstring for the measured cases
    where it reads 0 on a unit systemd itself printed a diagnostic for. The verdict comes from
    scanning stderr for a line matching `_FAIL_MESSAGE` and attributed either to `unit_path`'s
    own path or to a drop-in under `<unit_path>.d/`. systemd-analyze reads that directory
    beside the unit and names the drop-in's own path in its diagnostic, so a check keyed on
    the unit's path alone passed a typo'd drop-in.
    """
    proc = subprocess.run(
        [
            systemd_analyze_bin,
            "verify",
            "--man=no",
            "--recursive-errors=no",
            str(unit_path),
        ],
        capture_output=True,
        text=True,
    )
    prefixes = (f"{unit_path}:", f"{unit_path}.d/")
    hits = [
        line
        for line in proc.stderr.splitlines()
        if line.startswith(prefixes) and _FAIL_MESSAGE.search(line)
    ]
    if hits:
        return "\n".join(hits)
    return None


def check_template(
    path: Path, ctx: dict, out_dir: Path, systemd_analyze_bin: str
) -> str | None:
    """Render one unit template, write it under out_dir with its real unit name, and verify it.

    Returns an error string, or None on success.
    """
    rel = path.relative_to(ANSIBLE)
    env = template_env(path.parent)
    rendered, err = render_or_error(env, path.name, ctx)
    if rendered is None:
        return err

    out_path = out_dir / path.name.removesuffix(".j2")
    out_path.write_text(rendered)

    err = systemd_verify(out_path, systemd_analyze_bin)
    if err:
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"systemd-analyze verify: {err}"
    return None


def check_polkit_rule(template: Path, out_dir: Path, node_bin: str) -> str | None:
    """Render one polkit rule and `node --check` it. Returns an error string, or None.

    Uses the same StubUndefined + owning-role defaults context as the unit templates: a rule
    interpolates only a user name, which its role's own defaults carry.
    """
    ctx = unit_context(template)
    env = template_env(template.parent)
    rendered, err = render_or_error(env, template.name, ctx)
    if rendered is None:
        return err

    # .stem drops only the trailing ".j2" ("50-gitops-deploy.rules"); appending ".js" (not
    # ".rules") gives node a real JS extension without renaming the rule itself.
    out_path = out_dir / (template.stem + ".js")
    out_path.write_text(rendered)

    proc = subprocess.run(
        [node_bin, "--check", str(out_path)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        try:
            rel = template.relative_to(REPO)
        except ValueError:
            rel = template  # a test fixture outside REPO, not a real repo path
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"node --check: {proc.stderr.strip() or f'exited {proc.returncode}'}"
    return None


# The unit the host renders must emit, so a render that emits nothing fails rather than
# verifying zero units (H3, *Verification*, Unit validator).
HOST_NAMED_MEMBER = "homelab-updater.timer"

# Directories under `hosts/` that hold no host package.
_NOT_A_HOST = frozenset({"components", "packages", "lib", "keys"})


def render_component(name: str, hosts_root: Path, scratch: Path) -> host_mode.Package:
    """Component `name` rendered through a package of its own, built under `scratch`.

    A component no host package lists yet still ships, in the host package that later lists
    it, so its units are verified now. The scratch tree links the real component,
    `hosts/lib/` and `hosts/keys/`; the renderer resolves links before its containment checks.
    """
    root = scratch / "hosts"
    (root / "components").mkdir(parents=True)
    (root / "components" / name).symlink_to(
        (hosts_root / "components" / name).resolve()
    )
    (root / "lib").symlink_to((hosts_root / "lib").resolve())
    (root / "keys").symlink_to((hosts_root / "keys").resolve())
    package = f"component-{name}"
    spec = root / "packages" / package / "package.yml"
    spec.parent.mkdir(parents=True)
    spec.write_text(f"components: [{name}]\n")
    return host_mode.render_package(package, hosts_root=root)


def host_unit_groups(
    hosts_root: Path, scratch: Path
) -> list[tuple[str, list[host_mode.Package]]]:
    """Each set of packages that installs together, labelled, as the host renderer emits it.

    Every host package is grouped with every shared package, so a host's drop-in over a
    shared unit, such as its `homelab-updater.timer` schedule, is verified over that unit.
    The `shared` group holds the shared packages and every component no package lists.
    """
    shared_specs = sorted((hosts_root / "packages").glob("*/package.yml"))
    shared = [
        host_mode.render_package(spec.parent.name, hosts_root=hosts_root)
        for spec in shared_specs
    ]
    host_specs = [
        spec
        for spec in sorted(hosts_root.glob("*/package.yml"))
        if spec.parent.name not in _NOT_A_HOST
    ]
    listed = {
        component
        for spec in shared_specs + host_specs
        for component in load_yaml(spec).get("components") or []
    }
    orphans = [
        render_component(spec.parent.name, hosts_root, scratch / spec.parent.name)
        for spec in sorted((hosts_root / "components").glob("*/component.yml"))
        if spec.parent.name not in listed
    ]
    groups = [("shared", shared + orphans)]
    groups += [
        (
            spec.parent.name,
            shared + [host_mode.render_host(spec.parent.name, hosts_root=hosts_root)],
        )
        for spec in host_specs
    ]
    return groups


def check_host_group(
    packages: list[host_mode.Package], out_dir: Path, systemd_analyze_bin: str
) -> tuple[list[str], list[tuple[str, str]]]:
    """Lay out one group's unit directory under `out_dir` and verify every unit in it.

    Returns the units verified, and each failure as `(unit, error)`. A drop-in whose target
    unit no package in the group ships is verified over a stub unit of that name, which
    carries nothing the drop-in could be blamed for.
    """
    units: list[str] = []
    for package in packages:
        units += package.units()
        for dst, (content, _) in package.files.items():
            path = PurePosixPath(dst)
            if path.is_relative_to(host_mode.UNIT_DIR):
                target = out_dir / path.relative_to(host_mode.UNIT_DIR)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    for dropin_dir in sorted(out_dir.glob("*.d")):
        unit = dropin_dir.name.removesuffix(".d")
        if unit.endswith(host_mode.UNIT_SUFFIXES) and unit not in units:
            (out_dir / unit).write_text("[Unit]\nDescription=stub\n")
            units.append(unit)
    failures = []
    for unit in sorted(units):
        err = systemd_verify(out_dir / unit, systemd_analyze_bin)
        if err:
            print(f"\n----- rendered {unit} -----", file=sys.stderr)
            dump_numbered((out_dir / unit).read_text())
            failures.append((unit, f"systemd-analyze verify: {err}"))
    return sorted(units), failures


def check_host_units(
    hosts_root: Path, out_dir: Path, systemd_analyze_bin: str, ok
) -> tuple[int, int]:
    """Verify every unit the host renderer emits; return (units checked, failures).

    A render that raises, or a render missing `HOST_NAMED_MEMBER`, counts as a failure.
    """
    try:
        groups = host_unit_groups(hosts_root, out_dir / "components")
    except (host_mode.RenderRefused, host_mode.RenderError) as exc:
        print(f"  [FAIL] hosts/: the host render failed: {exc}", file=sys.stderr)
        return 0, 1
    checked = failures = 0
    seen: set[str] = set()
    for label, packages in groups:
        units, failed = check_host_group(
            packages, out_dir / "hosts" / label, systemd_analyze_bin
        )
        checked += len(units)
        seen.update(units)
        failures += len(failed)
        for unit, err in failed:
            print(f"  [FAIL] hosts/{label}: {unit}: {err}", file=sys.stderr)
        for unit in sorted(set(units) - {unit for unit, _ in failed}):
            ok(f"  [ok]   hosts/{label}: {unit}")
    if HOST_NAMED_MEMBER not in seen:
        print(
            f"  [FAIL] hosts/: no render emitted {HOST_NAMED_MEMBER}, so the host units "
            "were not checked",
            file=sys.stderr,
        )
        failures += 1
    return checked, failures


def main(argv: Sequence[str] = ()) -> int:
    """Render every discovered unit template, then `systemd-analyze verify` the output.

    Also renders and `node --check`s every polkit rule (`discover_rules`) — a missing `node`
    skips those checks with a message rather than failing the whole run closed (see module
    docstring for why the two missing-tool cases are handled differently).

    Args:
        argv: The command-line arguments. `-v` or `--verbose` also lists each clean
            template as an `[ok]` line; by default only failures and the summary print.

    Returns:
        0 if every unit rendered clean and verified clean, and every polkit rule either rendered
        clean or was skipped, 1 otherwise (including when systemd-analyze is missing from PATH
        or no unit templates were found).
    """
    ok = ok_printer(argv)
    systemd_analyze_bin = shutil.which("systemd-analyze")
    if not systemd_analyze_bin:
        print(
            "[FAIL] systemd-analyze not found on PATH. Failing closed rather than silently "
            "skipping the render — a template with a typo'd key must not read as checked when "
            "nothing checked it.",
            file=sys.stderr,
        )
        return 1

    templates = discover_templates()
    if not templates:
        print(
            f"No *.service.j2 / *.timer.j2 templates found under {ROLES}",
            file=sys.stderr,
        )
        return 1

    failures = 0
    with tempfile.TemporaryDirectory(prefix="validate-unit-templates-") as tmp:
        out_dir = Path(tmp)
        for path in templates:
            rel = path.relative_to(REPO)
            ctx = unit_context(path)
            err = check_template(path, ctx, out_dir, systemd_analyze_bin)
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                ok(f"  [ok]   {rel}")

        rules = discover_rules()
        node_bin = shutil.which("node")
        for rule in rules:
            try:
                rules_rel = rule.relative_to(REPO)
            except ValueError:
                rules_rel = rule  # a test fixture outside REPO, not a real repo path
            if not node_bin:
                print(
                    f"  [skip] {rules_rel}: node not on PATH — polkit rule JS syntax not "
                    "checked this run"
                )
                continue
            err = check_polkit_rule(rule, out_dir, node_bin)
            if err:
                failures += 1
                print(f"  [FAIL] {rules_rel}: {err}", file=sys.stderr)
            else:
                ok(f"  [ok]   {rules_rel}")

        host_units, host_failures = check_host_units(
            HOSTS, out_dir / "host-packages", systemd_analyze_bin, ok
        )
        failures += host_failures

    rules_checked = len(rules) if node_bin else 0
    print(
        f"\n{len(templates)} unit template(s), {host_units} host package unit(s) and "
        f"{rules_checked} polkit rule(s) checked, {failures} failure(s)."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
