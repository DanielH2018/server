#!/usr/bin/env python3
"""Render every systemd unit template under ansible/roles/ and verify the output.

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

Also renders every polkit rule (`*.rules.j2`) under ansible/roles/ — JavaScript that the
roles' tests content-check but whose syntax nothing else checks — and runs `node --check` on
each output. A missing `node` SKIPS with a message rather than failing closed, unlike a missing
`systemd-analyze`: `systemd-analyze` is this repo's only way to verify a unit file, so its
absence must not read as "checked"; the polkit rules are checked by a tool GitHub's own runner
images always carry (they run Actions itself on Node.js), so the skip path is a
local-workstation fallback, not a gate this repo depends on being armed in CI.

Run directly or via the ``validate-unit-templates`` prek hook. Exits non-zero if any unit fails
to render, if `systemd-analyze` reports a matching diagnostic, if a polkit rule fails to
render, or if `node --check` flags one. Exits non-zero if `systemd-analyze` itself isn't
available on PATH (fail loud, matching `shell_templates.py`'s policy — a missing verifier must
not silently degrade to "renders, so it's fine").
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.ansible_jinja_env import template_env
from lib.render_context import render_context
from lib.render_guard import (
    ANSIBLE,
    REPO,
    dump_numbered,
    render_or_error,
)

ROLES = ANSIBLE / "roles"

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
    scanning stderr for a line attributed to `unit_path`'s own path that matches `_FAIL_MESSAGE`.
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
    prefix = f"{unit_path}:"
    hits = [
        line
        for line in proc.stderr.splitlines()
        if line.startswith(prefix) and _FAIL_MESSAGE.search(line)
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


def main() -> int:
    """Render every discovered unit template, then `systemd-analyze verify` the output.

    Also renders and `node --check`s every polkit rule (`discover_rules`) — a missing `node`
    skips those checks with a message rather than failing the whole run closed (see module
    docstring for why the two missing-tool cases are handled differently).

    Returns:
        0 if every unit rendered clean and verified clean, and every polkit rule either rendered
        clean or was skipped, 1 otherwise (including when systemd-analyze is missing from PATH
        or no unit templates were found).
    """
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
                print(f"  [ok]   {rel}")

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
                print(f"  [ok]   {rules_rel}")

    rules_checked = len(rules) if node_bin else 0
    print(
        f"\n{len(templates)} unit template(s) and {rules_checked} polkit rule(s) checked, "
        f"{failures} failure(s)."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
