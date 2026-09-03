#!/usr/bin/env python3
"""Render every systemd unit template under ansible/roles/ and verify the output.

`shell_templates.py` covers `*.sh.j2` and `k8s_manifests.py` covers `roles/k8s/*/templates/*.j2`;
`*.service.j2` / `*.timer.j2` fell between them — nothing rendered a unit template or checked the
result, so a typo'd directive key reached a live host with `daemon_reload: true` reporting
success and systemd loading the unit anyway, ignoring the bad line with only a journal warning
(GitHub issue #948).

Render context = StubUndefined (`scripts.lib.render_guard`) PLUS the OWNING ROLE's
`defaults/main.yml` loaded as real values, plus `ansible/inventory/group_vars/all.yml`. Bare
stubs are not enough: `systemd-analyze verify` treats `OnCalendar=STUB` and
`OnUnitActiveSec=STUB` as parse failures, so 3 of the 17 live timers (gitops-deploy,
renovate-agent, claude-rc-restart) were red on a clean repo before their role's real schedule
value was layered in. What still renders as STUB: `inventory_hostname` and `ansible_managed`
(Ansible magic vars with no plaintext fallback here — both are used only in comments or
human-facing alert text, never in a directive systemd parses) and any default whose OWN value is
itself an unrendered Jinja reference to a var this script does not set (e.g.
`renovate_agent_repo_dir: "/home/{{ sys_user }}/server"` — loading defaults with `yaml.safe_load`
reads that as a literal string, so the rendered unit carries the literal text `{{ sys_user }}`
rather than a resolved path; harmless here since a `WorkingDirectory=` value's syntax doesn't
depend on what's inside it).

`systemd-analyze verify`'s EXIT STATUS is ignored — measured on systemd 255.4, it is 0 on
`SuccessExitStatuss=75` (a typo'd key) and on `TimeoutStartSec=6zz0min` (an unparsable value) on
at least one host, even though both print a diagnostic line. The only reliable signal is stderr:
a line attributed to the rendered unit's own path (`<tmp>/<unit>:<line>: ...`) matching
`Unknown key name|Failed to parse|Assignment outside of section`. `--man=no --recursive-errors=no`
drops `k3s.service: Failed to open ... Permission denied` noise from a followed `After=`/`Wants=`
target that systemd-analyze cannot read outside its normal unit search path, and a bare
`Command ... is not executable` (no `path:line:` prefix, so it never matches the attribution
check) is deliberately not a failure — this hook proves the unit PARSES, not that its ExecStart
binary exists on the render host.

Structural check only, same scope as `shell_templates.py`: this catches a typo'd DIRECTIVE KEY,
not a wrong value (`TimeoutStartSec=45min` when the intent was 60) or a misspelled `OnFailure=`
TARGET — `--recursive-errors=no` suppresses the line that would name a target unit systemd can't
find.

Run directly or via the ``validate-unit-templates`` prek hook. Exits non-zero if any unit fails
to render, if `systemd-analyze` reports a matching diagnostic, or if `systemd-analyze` itself
isn't available on PATH (fail loud, matching `shell_templates.py`'s policy — a missing verifier
must not silently degrade to "renders, so it's fine").
"""

from __future__ import annotations

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

from lib.render_guard import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    REPO,
    SHARED_TPL,
    dump_numbered,
    load_yaml,
    make_env,
    render_or_error,
)

ROLES = ANSIBLE / "roles"

# A line systemd-analyze attributes to the file it's checking looks like
# "<path>:<lineno>: <message>". A line about a followed unit ("k3s.service: Failed to open...")
# or an ExecStart binary check ("notreal.service: Command ... is not executable") carries no
# ":<lineno>:" — that shape difference is what keeps this from flagging either.
_FAIL_MESSAGE = re.compile(
    r"Unknown key name|Failed to parse|Assignment outside of section"
)


def discover_templates() -> list[Path]:
    """Return every live *.service.j2 / *.timer.j2 under ansible/roles/.

    Excludes ansible/roles/containers/archive/ — nothing there is installed by any play, the
    same exclusion `shell_templates.cron_job_scripts` applies to archived cron targets.
    """
    templates = [*ROLES.rglob("*.service.j2"), *ROLES.rglob("*.timer.j2")]
    return sorted(p for p in templates if "archive" not in p.relative_to(ROLES).parts)


def owning_role_defaults(template: Path) -> Path:
    """The `defaults/main.yml` of the role that ships `template`.

    Every live unit template sits directly under `<role>/templates/`, so the role directory is
    always the template's grandparent.
    """
    return template.parent.parent / "defaults" / "main.yml"


def render_context(template: Path) -> dict:
    """StubUndefined base context, plus the owning role's real defaults layered on top.

    Role defaults come last so a role's own value wins over the generic BASE_CONTEXT/all.yml
    fallback on a name collision (none exist today, by Ansible's role-prefix naming convention).
    """
    return {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(owning_role_defaults(template)),
    }


def build_env(template_dir: Path):
    return make_env([template_dir, SHARED_TPL])


def render_template(path: Path, ctx: dict) -> str:
    env = build_env(path.parent)
    rendered, err = render_or_error(env, path.name, ctx)
    if rendered is None:
        raise RuntimeError(err)
    return rendered


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
    env = build_env(path.parent)
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


def main() -> int:
    """Render every discovered unit template, then `systemd-analyze verify` the output.

    Returns:
        0 if every unit rendered clean and verified clean, 1 otherwise (including when
        systemd-analyze is missing from PATH or no templates were found).
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
            ctx = render_context(path)
            err = check_template(path, ctx, out_dir, systemd_analyze_bin)
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                print(f"  [ok]   {rel}")

    print(f"\n{len(templates)} unit template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
