#!/usr/bin/env python3
"""Render every Jinja-templated shell script under ansible/roles/ with stubbed vars and lint
the output (`bash -n` + shellcheck).

The prek `bash-syntax-check` / shellcheck hooks gate plain shell files (via identify's
shebang-aware `types = ["shell"]`), but identify tags a `*.sh.j2` template as `{jinja, text}` —
never `shell` — so a Jinja-templated script (e.g. an entrypoint or cron script) is invisible to
both gates no matter how badly it's broken. This is the same render-then-lint pattern as
`validate_compose_templates.py` / `validate_config_templates.py`, extended from YAML parsing to
shell linting: render structure with stubbed vars, then prove the OUTPUT is valid shell.

Structural check only: SOPS secrets and other runtime vars are stubbed (StubUndefined, plus a
small override map for values that need to be shell-plausible — see SHELL_STUB_OVERRIDES), so no
SOPS access is needed. Run directly or via the ``validate-shell-templates`` prek hook. Exits
non-zero if any template fails to render, fails `bash -n`, fails shellcheck, or if shellcheck
itself isn't available (a missing linter degrades the gate silently otherwise — fail loud
instead of falling back to bash -n alone).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib._render_guard import (  # noqa: E402
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


def _ansible_search(value, pattern, ignorecase=False, multiline=False) -> bool:
    """Mirror Ansible's `search` Jinja test (ansible.plugins.test.core) — a plain regex search,
    not a full match. Vanilla Jinja2 has no `search` test, so any template using Ansible's
    `search` (e.g. `list | reject('search', pattern)`) would otherwise fail to render here
    with `TemplateRuntimeError: No test named 'search'`. No current template needs it
    (docker-user-rules.sh.j2, the last one that did, retired at E7 2026-08-13) — kept
    registered so the next one that does just works."""
    flags = (re.I if ignorecase else 0) | (re.M if multiline else 0)
    return bool(re.search(pattern, str(value), flags))


_BOOLEANS_TRUE = {"y", "yes", "on", "1", "true", "t"}
_BOOLEANS_FALSE = {"n", "no", "off", "0", "false", "f", ""}


def _ansible_bool(value) -> bool:
    """Mirror Ansible's `bool` filter (module_utils.parsing.convert_bool.boolean, strict=False).

    Vanilla Jinja2 has no `bool` filter, so a template guarding on `x | bool` renders here with
    `TemplateRuntimeError: No filter named 'bool' found`. Faithfulness matters more than
    convenience: the reason templates use `| bool` at all is that `-e var=false` arrives as the
    STRING "false", which plain Jinja truthiness reads as True. A stub that just called Python's
    bool() would agree with Ansible on real booleans and disagree on exactly the inputs the
    filter exists for, so the test would pass while production took the other branch.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalised = str(value).strip().lower()
    if normalised in _BOOLEANS_TRUE:
        return True
    if normalised in _BOOLEANS_FALSE:
        return False
    return False


ROLES = ANSIBLE / "roles"

# Shell-specific overrides: values a lint pass needs to be shell-plausible rather than the bare
# "STUB" literal StubUndefined fills in elsewhere. A bare "STUB" would actually be fine for a
# plain string interpolation, but these three are structurally different:
#  - the two push tokens are SOPS secrets with no plaintext fallback in group_vars/all.yml (unlike
#    cloudflare_ips/sys_user below, which ARE plaintext and come through BASE_CONTEXT/all_vars
#    unchanged) — any token-shaped string is fine, they're just interpolated into a URL path.
#  - `hostvars` is Ansible's own magic var (host facts keyed by inventory hostname), not something
#    vanilla Jinja provides. pi-sd-health.sh.j2 only dereferences hostvars['daniel-server'].server_ip,
#    so stub just that path — Jinja's `.attr` lookup falls back to `dict.__getitem__` when the
#    attribute doesn't exist, so a plain nested dict renders identically to the real Ansible object.
SHELL_STUB_OVERRIDES = {
    "secret_rotation_push_token": "stub-secret-rotation-token",
    "pi_sd_health_push_token": "stub-pi-sd-health-token",
    "hostvars": {"daniel-server": {"server_ip": "10.0.0.1"}},
}


def discover_templates() -> list[Path]:
    """Every *.sh.j2 under ansible/roles/ (real templates only — ansible/collections/ is the
    vendored third-party tree and is excluded the same way pytest's testpaths / ruff's
    extend-exclude skip it)."""
    return sorted(ROLES.rglob("*.sh.j2"))


# cron's PATH (/usr/bin:/bin) omits /usr/local/bin, where the k3s install script puts `k3s`
# (and the `kubectl` it aliases). A script that calls either by bare name only works when RUN
# INTERACTIVELY, where the shell's own PATH already has it — under cron it fails "command not
# found", or worse: bare `kubectl` alone (without `k3s kubectl`) silently reports an EMPTY
# cluster rather than erroring, which reads as "the cluster has nothing" instead of "PATH is
# wrong". longhorn-trim-volumes.sh.j2 already carries the fix and the comment this trap is
# copied from.
#
# Only a script that's an ACTUAL cron `job:` target is in scope — this must not flag
# longhorn-reap-orphan-backups.sh.j2, which uses the same bare `k3s kubectl` and is deliberately
# UNSCHEDULED (health-crons.yml says so explicitly): a template-content scan alone can't tell
# "runs under cron's PATH" from "would, if anything ever scheduled it".
# No leading `\b` before the literal path: PATH= is always followed directly by `/` or a quote,
# neither a word character, so `\b` never matches there (a boundary needs one word side) — it
# would silently reject every real `export PATH=/usr/local/bin...` and `export PATH="/usr/local/
# bin...` line, which is exactly the two forms actually in use.
_PATH_EXPORT_INCLUDES_LOCAL_BIN = re.compile(
    r"^\s*export\s+PATH=[^\n]*/usr/local/bin\b", re.MULTILINE
)
# A real invocation, not a substring. Every script in this repo that touches the cluster does
# so via the compound `k3s kubectl ...` — k3s's own bundled wrapper — never a standalone
# `kubectl`; `k3s` is the actual binary PATH has to resolve, and the `kubectl` after it is just
# k3s's own subcommand syntax, not a second lookup. So only a BARE `k3s` is checked:
#  - `(?![\w/])`/`(?<![\w/])` rule out a preceding/following path separator (already-absolute,
#    e.g. `/usr/local/bin/k3s kubectl`) and a word character (a `k3s_`-prefixed Ansible var name
#    is not an invocation of anything).
# A bare `kubectl` alone is deliberately NOT matched: registry-gc.sh.j2 echoes one in a
# human-facing suggestion string ("see: kubectl -n ... logs ..."), which is not an invocation at
# all — matching it there would flag prose, not code. This is what must also not fire on
# longhorn-trim-volumes.sh.j2's own explanatory comment about this exact trap — comments are
# stripped before this runs, so prose mentioning either name never reaches it regardless.
_BARE_K8S_INVOCATION = re.compile(r"(?<![\w/])k3s(?![\w/])")
# `env: yes/true` + `name: PATH` is the ansible.builtin.cron idiom for a crontab-level `PATH=...`
# line (distinct from the module's `job:` — see the ansible.builtin.cron docs on `env`), which
# would fix every job in that cron_file without an in-script export. No cron task in this repo
# uses it today (checked 2026-08-17), so this branch is currently unexercised — kept as a real
# alternative rather than assuming every fix must be an in-script export.
_CRONTAB_PATH_ENV = re.compile(
    r"env:\s*(?:yes|true)\b[\s\S]{0,200}?/usr/local/bin|/usr/local/bin[\s\S]{0,200}?env:\s*(?:yes|true)\b"
)
# A cron `job:` naming a wrapper, with any leading `VAR=value` assignments captured rather than
# rejected. The regex used to demand the job be EXACTLY the script path, which silently put
# docs-refresh.sh — whose job line sets PATH and KUBECONFIG inline — outside every rule below.
# A guard that skips the one job doing something interesting with its environment is the guard
# scope drifting from the hazard, so the assignments are parsed and consulted instead.
_CRON_JOB_TARGET = re.compile(
    r"^(?P<env>(?:\w+=\S+\s+)*)/usr/local/bin/(?P<script>[\w.-]+\.sh)$"
)
# Root reads /etc/rancher/k3s/k3s.yaml automatically; nobody else can. Measured 2026-08-27 on
# daniel-box: the file is 0640 root:root, so `ubuntu` cannot read it. `ansible.builtin.cron`
# defaults `user` to the connection user (ubuntu here), so a MISSING user: is non-root and must
# provide KUBECONFIG — this fails closed on the omission rather than assuming root.
_CRON_ROOT_USER = "root"
# Unlike the PATH rule, an absolute path does NOT excuse this one: /usr/local/bin/k3s still
# needs a kubeconfig it can read. Two of the three KUBECONFIG-less wrappers invoke it that way.
_K8S_INVOCATION_ANY = re.compile(r"(?<![\w.-])(?:/usr/local/bin/)?k3s(?![\w/])")
_KUBECONFIG_ASSIGNED = re.compile(r"^\s*(?:export\s+)?KUBECONFIG=", re.MULTILINE)
_CRONTAB_KUBECONFIG_ENV = re.compile(
    r"env:\s*(?:yes|true)\b[\s\S]{0,200}?KUBECONFIG|KUBECONFIG[\s\S]{0,200}?env:\s*(?:yes|true)\b"
)


_JINJA_EXPR = re.compile(r"\{\{.*?\}\}")


def _collapse_jinja(text: str) -> str:
    """Replace `{{ ... }}` with a space-free token so word-splitting regexes still work.

    `KUBECONFIG=/home/{{ sys_user }}/.kube/config` contains spaces INSIDE the expression, so a
    `\\S+` value pattern stops at `{{` and the whole job line fails to match. That is how
    docs-refresh.sh — the one cron job setting both PATH and KUBECONFIG inline — sat outside
    every cron rule here.
    """
    return _JINJA_EXPR.sub("JINJA", text)


def _template_pairs(task: dict, mod: dict):
    """Yield (src, dest) for a template task, whether it names them directly or loops.

    The looped form — `src: "{{ item.src }}"` over a `loop:` of src/dest dicts — was invisible
    to this resolver until 2026-08-27, and it is the form k3s uses for EVERY longhorn wrapper.
    So the cron rules silently skipped exactly the scripts that talk to the cluster: they read
    `[ok]` because no rule applied, not because they satisfied one. Widening the parser is what
    makes the rules reach the hazard they were written for.
    """
    src, dest = str(mod.get("src", "")), str(mod.get("dest", ""))
    if "{{" not in src and "{{" not in dest:
        yield src, dest
        return
    items = task.get("loop") or task.get("with_items") or []
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict) and "src" in item and "dest" in item:
            yield str(item["src"]), str(item["dest"])


def _cron_targets(roles: Path = ROLES):
    """Yield (template_path, task_file, cron_task) for every cron-scheduled shell template.

    Two hops, not one: the deployed script's basename does not always match the template's own
    filename — claude-otel deploys templates/telemetry-health.sh.j2 to
    /usr/local/bin/claude-otel-health.sh (a `dest:` rename), and the cron `job:` only ever names
    the dest. So this resolves `job:` -> dest basename -> the `ansible.builtin.template` task in
    the SAME file whose `dest:` matches -> that task's `src:`, and only then has a template.

    archive/ is excluded — nothing there is included by any play, so its cron tasks never
    actually run.

    The single walk exists so `cron_job_scripts` and `cron_kubeconfig_error` cannot disagree
    about which templates are cron targets. They ask different questions of the same task, and
    a guard whose selector drifts from its sibling's is how a rule ends up covering less than
    the hazard it names.
    """
    # Roles are nested two levels under ROLES (roles/{containers,k8s,setup}/<role>/tasks/...),
    # so this needs rglob, not a fixed-depth glob.
    for task_file in sorted(roles.rglob("tasks/*.yml")):
        if "archive" in task_file.parts:
            continue
        try:
            tasks = yaml.safe_load(task_file.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(tasks, list):
            continue

        dest_to_src: dict[str, str] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            mod = task.get("ansible.builtin.template")
            if not isinstance(mod, dict):
                continue
            for src, dest in _template_pairs(task, mod):
                if src.endswith(".sh.j2") and dest.startswith("/usr/local/bin/"):
                    dest_to_src[Path(dest).name] = src

        for task in tasks:
            if not isinstance(task, dict):
                continue
            mod = task.get("ansible.builtin.cron")
            if not isinstance(mod, dict):
                continue
            job = _collapse_jinja(str(mod.get("job", "")).strip())
            m = _CRON_JOB_TARGET.match(job)
            if not m:
                continue
            src = dest_to_src.get(m.group("script"))
            if not src:
                continue
            template_path = task_file.parent.parent / "templates" / src
            # The leading `VAR=value` assignments are parsed HERE, once, so a consumer cannot
            # re-derive them from the raw job and disagree about what the line sets.
            yield template_path, task_file, mod, m.group("env")


def cron_job_scripts(roles: Path = ROLES) -> dict[Path, Path]:
    """Map template path -> the tasks/*.yml file that schedules it as a cron `job:`."""
    return {template: task_file for template, task_file, _, _ in _cron_targets(roles)}


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def cron_path_error(
    template: Path, rendered: str, cron_map: dict[Path, Path]
) -> str | None:
    """None if this template is fine; an error string if it's a cron job target that calls
    kubectl/k3s bare and has no PATH fix anywhere (in-script export, or a crontab PATH line)."""
    task_file = cron_map.get(template)
    if task_file is None:
        return None  # not a cron job target at all — out of scope for this rule

    code = _strip_comments(rendered)
    if _PATH_EXPORT_INCLUDES_LOCAL_BIN.search(code):
        return None
    if not _BARE_K8S_INVOCATION.search(code):
        return None
    if _CRONTAB_PATH_ENV.search(task_file.read_text()):
        return None

    try:
        rel_task_file = task_file.relative_to(REPO)
    except ValueError:
        rel_task_file = (
            task_file  # e.g. a unit test's tmp_path fixture, not a real repo path
        )
    return (
        "calls kubectl/k3s bare but is a cron job target "
        f"({rel_task_file}) — cron's PATH omits /usr/local/bin. Add "
        '`export PATH="/usr/local/bin:${PATH}"` (see longhorn-trim-volumes.sh.j2), call k3s/'
        "kubectl by absolute path, or set a crontab-level PATH via env: yes on the cron task."
    )


def cron_kubeconfig_error(
    template: Path, rendered: str, roles: Path = ROLES
) -> str | None:
    """None if this template is fine; an error string if a NON-ROOT cron job target touches the
    cluster without a KUBECONFIG it can read.

    The sibling of `cron_path_error`, and the trap its own memory entry predicted would still be
    live once the PATH half was fixed: "cron inherits neither PATH nor KUBECONFIG". The two fail
    differently, which is why one check cannot cover both. A missing PATH makes k3s not resolve,
    so the script dies loudly. A missing KUBECONFIG resolves the binary fine and then reports an
    EMPTY CLUSTER — zero pods, zero volumes, nothing wrong — so a health check built on it goes
    green while seeing nothing at all.

    Root is exempt because k3s writes /etc/rancher/k3s/k3s.yaml as 0640 root:root and reads it
    by default; `ubuntu` cannot open it. `ansible.builtin.cron` defaults `user` to the connection
    user, so a task with no `user:` is treated as non-root and must set KUBECONFIG.

    The user is read from the SPECIFIC cron task scheduling this template, never by searching the
    task file. health-crons.yml schedules eight crons with different users, so a file-wide search
    would let one task's `user: root` excuse a sibling non-root task — the guard reading green on
    exactly the case it exists to catch.

    Verify a suspected instance by running the wrapper the way cron does:
    `scripts/dev/run_as_cron.sh --expect-output /usr/local/bin/<wrapper>.sh`, which exits 66 on
    the clean-exit-no-output signature this fault produces.
    """
    code = _strip_comments(rendered)
    if not _K8S_INVOCATION_ANY.search(code):
        return None

    for tpl, task_file, cron_task, job_env in _cron_targets(roles):
        if tpl != template:
            continue
        if str(cron_task.get("user", "")).strip() == _CRON_ROOT_USER:
            return None
        if "KUBECONFIG=" in job_env:
            return None
        if _KUBECONFIG_ASSIGNED.search(code):
            return None
        if _CRONTAB_KUBECONFIG_ENV.search(task_file.read_text()):
            return None
        try:
            rel_task_file = task_file.relative_to(REPO)
        except ValueError:
            rel_task_file = task_file
        user = (
            str(cron_task.get("user", "")).strip() or "the connection user (non-root)"
        )
        return (
            f"touches the cluster but is scheduled as {user} ({rel_task_file}) with no "
            "KUBECONFIG — cron does not inherit one, and k3s.yaml is root-only, so this "
            "reports an EMPTY cluster rather than failing. Set KUBECONFIG in the script, in "
            "the cron job: line, or via env: yes on the cron task — or schedule it as root "
            "(see crowdsec-appsec-verify.sh.j2)."
        )
    return None


def build_env(template_dir: Path) -> Environment:
    env = make_env([template_dir, SHARED_TPL])
    env.tests["search"] = _ansible_search
    env.filters["bool"] = _ansible_bool
    return env


def render_template(path: Path, ctx: dict) -> str:
    env = build_env(path.parent)
    rendered, err = render_or_error(env, path.name, ctx)
    if err:
        raise RuntimeError(err)
    return rendered


def bash_syntax_check(path: Path) -> str | None:
    """`bash -n` parses (never executes) the rendered script. Return an error string, or None."""
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return proc.stderr.strip() or f"bash -n exited {proc.returncode}"
    return None


def shellcheck_check(path: Path, shellcheck_bin: str) -> str | None:
    """Run shellcheck (all severities — the repo default, no --severity override, matching the
    prek shellcheck hook) against the rendered script. Return an error string, or None."""
    proc = subprocess.run([shellcheck_bin, str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return (
            proc.stdout.strip()
            or proc.stderr.strip()
            or f"shellcheck exited {proc.returncode}"
        )
    return None


def check_template(
    path: Path,
    ctx: dict,
    out_dir: Path,
    shellcheck_bin: str,
    cron_map: dict[Path, Path],
) -> str | None:
    """Render one template, write it under out_dir preserving its relative path (minus the
    trailing .j2), then lint the rendered file. Return an error string, or None on success."""
    rel = path.relative_to(ANSIBLE)
    env = build_env(path.parent)
    rendered, err = render_or_error(env, path.name, ctx)
    if err:
        return err

    out_path = out_dir / rel.with_suffix("")  # drop the trailing .j2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered)

    err = bash_syntax_check(out_path)
    if err:
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"bash -n: {err}"

    err = shellcheck_check(out_path, shellcheck_bin)
    if err:
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"shellcheck: {err}"

    err = cron_path_error(path, rendered, cron_map)
    if err:
        return err

    err = cron_kubeconfig_error(path, rendered)
    if err:
        return err

    return None


def main() -> int:
    shellcheck_bin = shutil.which("shellcheck")
    if not shellcheck_bin:
        print(
            "[FAIL] shellcheck not found on PATH. It ships via the `shellcheck-py` dev "
            "dependency (pyproject.toml [dependency-groups] dev) — run through `uv run "
            "python scripts/validate/validate_shell_templates.py` (or any `uv run ...`) so uv's synced "
            "venv is on PATH. Failing closed rather than silently degrading to bash -n alone.",
            file=sys.stderr,
        )
        return 1

    templates = discover_templates()
    if not templates:
        print(f"No *.sh.j2 templates found under {ROLES}", file=sys.stderr)
        return 1

    all_vars = load_yaml(ALL_VARS)
    ctx = {**BASE_CONTEXT, **all_vars, **SHELL_STUB_OVERRIDES}
    cron_map = cron_job_scripts()

    failures = 0
    with tempfile.TemporaryDirectory(prefix="validate-shell-templates-") as tmp:
        out_dir = Path(tmp)
        for path in templates:
            rel = path.relative_to(REPO)
            err = check_template(path, ctx, out_dir, shellcheck_bin, cron_map)
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                print(f"  [ok]   {rel}")

    print(f"\n{len(templates)} shell template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
