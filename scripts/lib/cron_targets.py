#!/usr/bin/env python3
"""Resolve which shell templates under `ansible/roles/` are scheduled as cron `job:` targets.

The cron rules in `scripts/lib/cron_checks.py` only apply to a script cron actually runs, so
the hard part is not the rule but the resolution: a cron `job:` names a path under
`/usr/local/bin/`, and the template that produced it may have a different basename, may be
deployed by a `loop:`, or may reach the host through `release_bin.yml`'s group indirection.
All three shapes are parsed here, once, so no two consumers can disagree about which
templates are in scope — a selector that drifts from its sibling's is how a guard ends up
covering less than the hazard it names.

The cron-rule regexes live here too, beside the resolution they are matched against.
`scripts/validate/shell_templates.py` is the entry point that runs the rules over the tree.
"""

import re
import sys
from pathlib import Path

import yaml

# A directly-invoked script gets only its own directory on sys.path, and pyproject's
# `pythonpath` is a pytest setting — so the cross-directory imports below need the
# scripts/ root here, the same way its siblings reach it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import release_bin_groups, yaml_fast
from lib.repo_paths import REPO, ROLES

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
PATH_EXPORT_INCLUDES_LOCAL_BIN = re.compile(
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
BARE_K8S_INVOCATION = re.compile(r"(?<![\w/])k3s(?![\w/])")
# `env: yes/true` + `name: PATH` is the ansible.builtin.cron idiom for a crontab-level `PATH=...`
# line (distinct from the module's `job:` — see the ansible.builtin.cron docs on `env`), which
# would fix every job in that cron_file without an in-script export. No cron task in this repo
# uses it today (checked 2026-08-17), so this branch is currently unexercised — kept as a real
# alternative rather than assuming every fix must be an in-script export.
CRONTAB_PATH_ENV = re.compile(
    r"env:\s*(?:yes|true)\b[\s\S]{0,200}?/usr/local/bin|/usr/local/bin[\s\S]{0,200}?env:\s*(?:yes|true)\b"
)
# A cron `job:` naming a wrapper, with any leading `VAR=value` assignments captured rather than
# rejected. The regex used to demand the job be EXACTLY the script path, which silently put
# docs-refresh.sh — whose job line sets PATH and KUBECONFIG inline — outside every rule below.
# A guard that skips the one job doing something interesting with its environment is the guard
# scope drifting from the hazard, so the assignments are parsed and consulted instead.
CRON_JOB_TARGET = re.compile(
    r"^(?P<env>(?:\w+=\S+\s+)*)/usr/local/bin/(?P<script>[\w.-]+\.sh)$"
)
# Root reads /etc/rancher/k3s/k3s.yaml automatically; nobody else can. Measured 2026-08-27 on
# daniel-box: the file is 0640 root:root, so `ubuntu` cannot read it. `ansible.builtin.cron`
# defaults `user` to the connection user (ubuntu here), so a MISSING user: is non-root and must
# provide KUBECONFIG — this fails closed on the omission rather than assuming root.
CRON_ROOT_USER = "root"
# Unlike the PATH rule, an absolute path does NOT excuse this one: /usr/local/bin/k3s still
# needs a kubeconfig it can read. Two of the three KUBECONFIG-less wrappers invoke it that way.
K8S_INVOCATION_ANY = re.compile(r"(?<![\w.-])(?:/usr/local/bin/)?k3s(?![\w/])")
KUBECONFIG_ASSIGNED = re.compile(r"^\s*(?:export\s+)?KUBECONFIG=", re.MULTILINE)
CRONTAB_KUBECONFIG_ENV = re.compile(
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


def _release_bin_pairs(task: dict, task_file: Path):
    """Yield (repo-relative src, dest) for a group deployed by release_bin.yml.

    A third deploy shape, after the direct `template:` and the looped one. A role hands
    release_bin.yml a GROUP NAME and the file list lives in that role's defaults, so nothing in
    the task file itself names a template — which made every script in a converted group
    invisible to this resolver. That is the same coverage loss the looped form caused before
    2026-08-27: the cron rules would read `[ok]` because no rule applied, not because the script
    satisfied one.

    The resolution lives in `scripts/lib/release_bin_groups.py` because the secrets guard needs
    the same answer, and two resolvers that can disagree about a group's contents is the defect
    this shape already caused once: `release_bin_templates` parses as a folded Jinja string, and
    the guard's own copy iterated it into nothing and passed having scanned zero files.

    The host name of a rendered script is its basename minus `.j2`, which is release_bin.yml's
    own rule for the same derivation. Only `.j2` sources yield a pair — the consumer filters on
    shell templates anyway, and a `files` entry may be renamed on the host.
    """
    for src in release_bin_groups.task_sources(task, task_file):
        if src.endswith(".j2"):
            yield src, f"/usr/local/bin/{release_bin_groups.host_name(src)}"


def iter_cron_targets(roles: Path = ROLES):
    """Yield (template_path, task_file, cron_task) for every cron-scheduled shell template.

    Two hops, not one: the deployed script's basename does not always match the template's own
    filename — claude-otel deploys templates/telemetry-health.sh.j2 to
    /usr/local/bin/claude-otel-health.sh (a `dest:` rename), and the cron `job:` only ever names
    the dest. So this resolves `job:` -> dest basename -> the `ansible.builtin.template` task in
    the SAME file whose `dest:` matches -> that task's `src:`, and only then has a template.

    archive/ is excluded — nothing there is included by any play, so its cron tasks never
    actually run.

    The single walk exists so `cron_job_scripts` and `cron_checks.cron_kubeconfig_error` cannot
    disagree about which templates are cron targets. They ask different questions of the same
    task, and a guard whose selector drifts from its sibling's is how a rule ends up covering
    less than the hazard it names.
    """
    # Roles are nested two levels under ROLES (roles/{containers,k8s,setup}/<role>/tasks/...),
    # so this needs rglob, not a fixed-depth glob.
    for task_file in sorted(roles.rglob("tasks/*.yml")):
        if "archive" in task_file.parts:
            continue
        try:
            tasks = yaml_fast.safe_load(task_file.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(tasks, list):
            continue

        # Maps to an absolute template PATH, not a bare src, because the two deploy shapes
        # resolve from different roots: a `template:` src is relative to the role's templates/
        # dir, while a release_bin src is repo-relative. Resolving at insert time, where the
        # shape is known, keeps that difference out of every consumer below.
        dest_to_src: dict[str, Path] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            for src, dest in _release_bin_pairs(task, task_file):
                if src.endswith(".sh.j2") and dest.startswith("/usr/local/bin/"):
                    dest_to_src[Path(dest).name] = REPO / src
            mod = task.get("ansible.builtin.template")
            if not isinstance(mod, dict):
                continue
            for src, dest in _template_pairs(task, mod):
                if src.endswith(".sh.j2") and dest.startswith("/usr/local/bin/"):
                    dest_to_src[Path(dest).name] = (
                        task_file.parent.parent / "templates" / src
                    )

        for task in tasks:
            if not isinstance(task, dict):
                continue
            mod = task.get("ansible.builtin.cron")
            if not isinstance(mod, dict):
                continue
            job = _collapse_jinja(str(mod.get("job", "")).strip())
            m = CRON_JOB_TARGET.match(job)
            if not m:
                continue
            template_path = dest_to_src.get(m.group("script"))
            if not template_path:
                continue
            # The leading `VAR=value` assignments are parsed HERE, once, so a consumer cannot
            # re-derive them from the raw job and disagree about what the line sets.
            yield template_path, task_file, mod, m.group("env")


def cron_job_scripts(roles: Path = ROLES) -> dict[Path, Path]:
    """Map template path -> the tasks/*.yml file that schedules it as a cron `job:`."""
    return {
        template: task_file for template, task_file, _, _ in iter_cron_targets(roles)
    }


def strip_comments(text: str) -> str:
    """Drop whole-line `#` comments, so a rule matches code rather than prose about it.

    longhorn-trim-volumes.sh.j2 explains the bare-`k3s` trap in a comment; without this the
    rules in `cron_checks.py` would flag the explanation as an instance of what it warns about.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
