#!/usr/bin/env python3
"""Check the alert plane's inputs with promtool, amtool, pint and lokitool, the tools that load them.

A `prometheus.yml` that Prometheus rejects at startup leaves the `Recreate` Deployment with no
running Prometheus, and monitor-bridge reads that Prometheus every cycle. Neither `check-yaml`
nor the k8s manifest validator can see it, because the file is valid YAML inside a valid
ConfigMap. Only Prometheus's own parser can, and promtool is that parser.

THE TOOL PHASE. The script renders the observability role through `validate.k8s_manifests`,
the render `probe.py health` uses, with secrets stubbed. It extracts each rendered file
`RENDERED` names and runs:

- `promtool check config --syntax-only` on `prometheus.yml`. `--syntax-only` skips the files
  the config references, such as the scrape-credential and service-account paths, which do
  not exist outside the pod.
- `amtool check-config` on `alertmanager.yml`, with `homelab.tmpl` written beside it, because
  the config names its template by that relative path. The credentials are `*_file` paths,
  which amtool does not open.
- `promtool check rules` on every rule file in the role's `files/rules/`.
- `promtool test rules` on every test file in the role's `tests/rules/`.
- `amtool template render` of each notification define in `homelab.tmpl` against each group
  under the role's `tests/alertmanager/`, compared with its expected text
  (`scripts/validate/notification_renders.py`).
- `pint --offline lint` on the role's `files/rules/`, with `scripts/validate/pint/offline.hcl`.
  pint checks what promtool does not, such as a required label's value. `--offline` skips every
  check that queries a Prometheus, because CI cannot reach the cluster;
  `scripts/validate/pint/live.hcl` is the by-hand run that does.

The script also renders the loki-homelab role's `loki-homelab-rules` ConfigMap and runs
`lokitool rules lint --dry-run` on every rule file in that role's `files/rules/` (THE LOKI
RULER, below).

The rendered `prometheus.yml` holds no stubbed value, so the stubbing changes nothing promtool
reads. The stubbed `alertmanager.yml` reads `email` as a placeholder address, which amtool
accepts as it accepts the real one. `FLOOR` names the rendered files the run must find. A
render that stopped producing one fails instead of checking nothing.

THE RULE CONVENTIONS. `scripts/validate/alert_conventions.py:rule_problems` holds the rule
files to the conventions promtool does not check, before the tools run. This script supplies
the rule groups and the rendered `prometheus-rules` keys from the role's render.

THE THIRD `service` SOURCE. An alert carries `service` as a literal, through M2's
workload-service join, or from its scrape job's targets. The third source is for an alert like
`probe_success{job="x"} == 0`, whose series carry `service` because every target of the job
sets it. `alert_conventions.service_from_job` and `rule_test_problems` accept an alert with no
literal `service` on this source only when all three conditions hold:

1. Every vector selector in its expression carries a `job` matcher by equality, and all of them
   name the same job. A regex matcher, a selector with no `job` matcher, or two jobs fail.
2. Every target of that job in the rendered `prometheus.yml` sets `service` in its `labels`.
   A job that discovers its targets sets no static label, so it fails, and so does a job with
   one target that lacks `service`. So does a job whose `relabel_configs` or
   `metric_relabel_configs` can drop or rewrite `service` or `job`, because the static label
   would then never reach the series.
3. Every `exp_alerts` entry for the alert in its promtool test lists `service` in
   `exp_labels`. promtool compares whole label sets, so an expression that drops `service`
   fails that case in the tool phase. An aggregation whose `by` omits it, a `without
   (service)`, an `absent()` and vector matching with `on()` all fail there, without a lexical
   check having to name each shape.

The script hands the rendered `prometheus.yml` to `rule_problems` for condition 2. The second
`promtool test rules` proof shows condition 3 rests on promtool: its bad fixture is an
aggregation whose firing case lists `service`, and promtool fails it because the fired alert
carries none.

THE LOKI RULER. loki-homelab's ruler evaluates LogQL rules from that role's `files/rules/`,
which `templates/rules-configmap.yaml.j2` carries into the `loki-homelab-rules` ConfigMap one
key per group in `loki_homelab_rule_groups`. Two checks cover them:

- `lokitool rules lint --dry-run` parses each file's LogQL. Without `--dry-run` lint rewrites
  the file, so `LOKITOOL_ARGS` carries the flag and no call site adds it.
- `alert_conventions.group_list_problems`, with the loki-homelab names, fails when the rule
  files' stems, `loki_homelab_rule_groups` and the rendered ConfigMap's keys are not one set.
  A file the list does not name never reaches the ruler. `LOKI_GROUP_FLOOR` names the group a
  run must find, so an emptied directory, list and ConfigMap fail rather than agree on nothing.

Nothing else in `rule_problems` reads that directory, and pint does not lint it. LogQL has no
`promtool test rules` counterpart, so the fires-in-a-test conventions cannot apply, and the
directory's separation from the observability role's `files/rules/` is the exemption M5's slice
plan names.

THE GO-TEMPLATE SURVIVAL CHECK. `alertmanager.yml` is part of a Jinja template, and every Go
template expression in it sits inside a Jinja raw block. Jinja refuses most Go expressions as
syntax errors, and the hook reports that failed render. A Go expression that Jinja can parse
is consumed instead, and the rendered file still passes amtool. `go_template_problems`
therefore requires three things of the render, none of which an edit to the Jinja source can
take away by rewriting the expression:

- the text of every raw block in the source, verbatim;
- `{{ template "<name>" . }}` for every template `homelab.tmpl` defines, so a reference
  rewritten outside its raw block is still required;
- `{{ .Status }}`, the Watchdog's payload.

THE TOOLS. `scripts/validate/alert_tools.sh` provisions promtool and amtool at the observability
role's image tags, lokitool at loki-homelab's, and pint at the version it pins, into
`alert-tools/` under the git common dir, which every worktree of a clone shares. Every tool call
goes through it.

`--prove-red` runs each tool command against `PROOFS`, the fixtures in
`scripts/validate/tests/fixtures/alert_rules/`. It exits 0 only when every tool accepts its
good fixture and rejects its bad one with the expected message. The accepting half proves the
tool ran at all: a missing binary also exits non-zero, and without the good fixture it would
read as a rejection. CI's `hooks` job runs this step after provisioning. The pytest shards
install no binaries, so that step is the tools' red proof.

Run directly or via the `validate-alert-rules` prek hook. Exits non-zero on any problem and
prints each one, with the tool's output, on stderr.
"""

import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser
from lib.repo_paths import K8S_ROLES, REPO
from validate.notification_renders import RENDER_CASES, render_problems
from validate.alert_conventions import (
    RULE_TESTS_DIR,
    RULES_CONFIGMAP,
    RULES_DIR,
    group_list_problems,
    rule_problems,
)

ROLE = "observability"
ROLE_DIR = K8S_ROLES / ROLE
TOOLS = REPO / "scripts" / "validate" / "alert_tools.sh"
FIXTURES = REPO / "scripts" / "validate" / "tests" / "fixtures" / "alert_rules"
PINT_OFFLINE = REPO / "scripts" / "validate" / "pint" / "offline.hcl"
# pint's arguments before the path it lints. INFO lines go to stderr and the colour codes would
# land in the problem text, so the run is quiet and plain.
PINT_ARGS = (
    "--no-color",
    "--log-level",
    "warn",
    "--offline",
    "--config",
    str(PINT_OFFLINE),
    "lint",
)

# Each rendered file the tool phase checks: the template that renders it, and the kind, name
# and data key of the object that carries it.
RENDERED = {
    "prometheus.yml": ("prometheus.yaml.j2", "ConfigMap", "prometheus-config"),
    "alertmanager.yml": (
        "alertmanager-secret.yaml.j2",
        "Secret",
        "alertmanager-config",
    ),
    "homelab.tmpl": ("alertmanager-secret.yaml.j2", "Secret", "alertmanager-config"),
}

# The rendered files a run must find. The M1 slice plan raises it again when the rule files
# land.
FLOOR = frozenset({"prometheus.yml", "alertmanager.yml", "homelab.tmpl"})

# The template whose Go expressions must survive the Jinja render, and what the survival
# check reads from it and from homelab.tmpl. `{{ .Status }}` is the Watchdog's payload and is
# required whatever the source holds.
ALERTMANAGER_SOURCE = ROLE_DIR / "templates" / "alertmanager-secret.yaml.j2"
WATCHDOG_STATUS = "{{ .Status }}"
RAW_BLOCK = re.compile(r"\{% raw %\}(.*?)\{% endraw %\}", re.DOTALL)
TEMPLATE_DEFINE = re.compile(r'\{\{-? define "([^"]+)" -?\}\}')

# The template that renders the rule files' ConfigMap.
RULES_TEMPLATE = "prometheus-rules.yaml.j2"

# The loki-homelab ruler's LogQL rules: the role, its rule directory, the template that
# renders their ConfigMap, that ConfigMap, the role default listing the groups, and the groups
# a run must find.
LOKI_ROLE = "loki-homelab"
LOKI_ROLE_DIR = K8S_ROLES / LOKI_ROLE
LOKI_RULES_DIR = LOKI_ROLE_DIR / "files" / "rules"
LOKI_RULES_TEMPLATE = "rules-configmap.yaml.j2"
LOKI_RULES_CONFIGMAP = "loki-homelab-rules"
LOKI_RULE_GROUPS = "loki_homelab_rule_groups"
LOKI_GROUP_FLOOR = frozenset({"homelab-logs"})
# lokitool's arguments before the rule file. `--dry-run` keeps lint from rewriting the file.
LOKITOOL_ARGS = ("rules", "lint", "--dry-run")


# A tool run: the argv after `alert_tools.sh`, returning (exit code, combined output).
Runner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class Proof:
    """One tool's red proof: a fixture it must accept and one it must reject.

    Attributes:
        tool: `promtool`, `amtool` or `pint`.
        args: The tool's arguments, ending before the fixture path.
        good: The fixture the tool must accept.
        bad: The fixture the tool must reject.
        marker: Text the rejection's output must hold, so a crash or a usage error does not
            pass as the validation failing.
    """

    tool: str
    args: tuple[str, ...]
    good: str
    bad: str
    marker: str


PROOFS = (
    Proof(
        "promtool",
        ("check", "rules"),
        "good-rules.yaml",
        "bad-rules.yaml",
        "parse error",
    ),
    Proof(
        "promtool",
        ("test", "rules"),
        "good-rules.test.yaml",
        "bad-rules.test.yaml",
        "alertname: FixtureTargetDown, time: 3m",
    ),
    Proof(
        "promtool",
        ("test", "rules"),
        "good-job-service-rules.test.yaml",
        "bad-job-service-rules.test.yaml",
        "alertname: FixtureProbesAggregated, time: 3m",
    ),
    Proof(
        "amtool",
        ("check-config",),
        "good-alertmanager.yml",
        "bad-alertmanager.yml",
        "undefined receiver",
    ),
    Proof(
        "promtool",
        ("check", "config", "--syntax-only"),
        "good-prometheus.yml",
        "bad-prometheus.yml",
        "not found in type",
    ),
    Proof(
        "pint",
        PINT_ARGS,
        "good-rules.yaml",
        "bad-pint-rules.yaml",
        "`severity` label is required",
    ),
    Proof(
        "lokitool",
        LOKITOOL_ARGS,
        "good-loki-rules.yaml",
        "bad-loki-rules.yaml",
        "could not parse expression",
    ),
)


def run_tool(argv: list[str]) -> tuple[int, str]:
    """Run `alert_tools.sh <argv>` and return its exit code and combined output."""
    proc = subprocess.run(
        [str(TOOLS), *argv],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def rendered_files() -> dict[str, str]:
    """The text of every file `RENDERED` names, from the role's offline render.

    A file whose object or key the render does not produce is left out, which `FLOOR` turns
    into a problem rather than a silent skip.

    Raises:
        RuntimeError: a template failed to render.
    """
    from validate import k8s_manifests as validator

    entries = validator.k8s_entries()
    ctx = validator.render_context(
        ROLE_DIR, overrides={"container_item": entries[ROLE]}, strict=True
    )
    found = {}
    for key, (template, kind, name) in RENDERED.items():
        err, docs = validator.check_template(
            ROLE, ROLE_DIR / "templates" / template, ctx
        )
        if err:
            raise RuntimeError(f"{ROLE}/{template} failed to render: {err}")
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != kind:
                continue
            if doc.get("metadata", {}).get("name") != name:
                continue
            values = doc.get("data") or doc.get("stringData") or {}
            if key in values:
                found[key] = values[key]
    return found


def floor_problems(found: Mapping[str, str]) -> list[str]:
    """A problem for each `FLOOR` file the render did not produce."""
    return [
        f"{key}: the render produced no such file, so nothing checked it"
        for key in sorted(FLOOR - found.keys())
    ]


def go_template_problems(source: str, rendered: str, templates: str) -> list[str]:
    """A problem for each Go template expression the render of `source` lost.

    Args:
        source: The text of the Jinja template that renders `alertmanager.yml`.
        rendered: The rendered `alertmanager.yml`.
        templates: The text of `homelab.tmpl`, whose every `define` the render must
            reference.
    """
    required = {
        WATCHDOG_STATUS,
        *(block for block in RAW_BLOCK.findall(source) if block.strip()),
        *(
            f'{{{{ template "{name}" . }}}}'
            for name in TEMPLATE_DEFINE.findall(templates)
        ),
    }
    if not TEMPLATE_DEFINE.search(templates):
        return [
            "homelab.tmpl: defines no template, so nothing says which references "
            "alertmanager.yml must keep"
        ]
    return [
        f"alertmanager.yml: the render lost `{expr}`. A Go expression outside a Jinja raw "
        "block is consumed by the render, and Alertmanager then sends an empty field."
        for expr in sorted(required)
        if expr not in rendered
    ]


def _rendered_group_keys(
    role: str, template: str, configmap: str, list_name: str
) -> tuple[list[str], set[str]]:
    """A role's rule-group list and the data keys of the rules ConfigMap it renders.

    Args:
        role: The k8s role.
        template: The template under the role's `templates/` that renders the ConfigMap.
        configmap: The ConfigMap's name.
        list_name: The role default that lists the groups.

    Raises:
        RuntimeError: the template failed to render.
    """
    from validate import k8s_manifests as validator

    role_dir = K8S_ROLES / role
    entries = validator.k8s_entries()
    ctx = validator.render_context(
        role_dir, overrides={"container_item": entries[role]}, strict=True
    )
    err, docs = validator.check_template(role, role_dir / "templates" / template, ctx)
    if err:
        raise RuntimeError(f"{role}/{template} failed to render: {err}")
    keys = set()
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        if doc.get("metadata", {}).get("name") == configmap:
            keys |= set(doc.get("data") or {})
    return list(ctx.get(list_name) or []), keys


def rendered_rule_keys() -> tuple[list[str], set[str]]:
    """`observability_rule_groups` and the rendered `prometheus-rules` ConfigMap's data keys.

    Raises:
        RuntimeError: the rules template failed to render.
    """
    return _rendered_group_keys(
        ROLE, RULES_TEMPLATE, RULES_CONFIGMAP, "observability_rule_groups"
    )


def rendered_loki_rule_keys() -> tuple[list[str], set[str]]:
    """`loki_homelab_rule_groups` and the rendered `loki-homelab-rules` ConfigMap's data keys.

    Raises:
        RuntimeError: the rules template failed to render.
    """
    return _rendered_group_keys(
        LOKI_ROLE, LOKI_RULES_TEMPLATE, LOKI_RULES_CONFIGMAP, LOKI_RULE_GROUPS
    )


def loki_group_problems(
    groups: list[str], keys: set[str], rules_dir: Path = LOKI_RULES_DIR
) -> list[str]:
    """A problem for each group the Loki rule files, their list and their ConfigMap disagree on.

    Args:
        groups: `loki_homelab_rule_groups`.
        keys: The rendered `loki-homelab-rules` ConfigMap's data keys.
        rules_dir: The Loki rule files.
    """
    return group_list_problems(
        {p.stem for p in rules_dir.glob("*.yaml")},
        groups,
        keys,
        floor=LOKI_GROUP_FLOOR,
        list_name=LOKI_RULE_GROUPS,
        configmap=LOKI_RULES_CONFIGMAP,
        rules_path=f"{LOKI_ROLE}/files/rules",
    )


def tool_checks(
    rendered: Mapping[str, Path],
    rules_dir: Path = RULES_DIR,
    tests_dir: Path = RULE_TESTS_DIR,
    loki_rules_dir: Path = LOKI_RULES_DIR,
) -> list[list[str]]:
    """The tool invocations for every discovered input, as `alert_tools.sh` argv lists."""
    checks = []
    if "alertmanager.yml" in rendered:
        checks.append(["amtool", "check-config", str(rendered["alertmanager.yml"])])
    if "prometheus.yml" in rendered:
        checks.append(
            [
                "promtool",
                "check",
                "config",
                "--syntax-only",
                str(rendered["prometheus.yml"]),
            ]
        )
    checks += [
        ["promtool", "check", "rules", str(p)] for p in sorted(rules_dir.glob("*.yaml"))
    ]
    checks += [
        ["promtool", "test", "rules", str(p)]
        for p in sorted(tests_dir.glob("*.test.yaml"))
    ]
    checks.append(["pint", *PINT_ARGS, str(rules_dir)])
    checks += [
        ["lokitool", *LOKITOOL_ARGS, str(p)]
        for p in sorted(loki_rules_dir.glob("*.yaml"))
    ]
    return checks


def check_tree(
    run: Runner = run_tool,
    render: Callable[[], dict[str, str]] | None = None,
    render_rules: Callable[[], tuple[list[str], set[str]]] | None = None,
    rules_dir: Path = RULES_DIR,
    tests_dir: Path = RULE_TESTS_DIR,
    render_loki_rules: Callable[[], tuple[list[str], set[str]]] | None = None,
    loki_rules_dir: Path = LOKI_RULES_DIR,
    render_cases: Path = RENDER_CASES,
) -> list[str]:
    """Render the role, apply the floors and the rule conventions, and run every tool check.

    Args:
        run: Runs one tool invocation.
        render: Returns the rendered files; `rendered_files` unless a test passes another.
        render_rules: Returns the rule groups and the rendered rule keys;
            `rendered_rule_keys` unless a test passes another.
        rules_dir: The rule files.
        tests_dir: Their promtool tests.
        render_loki_rules: Returns the Loki rule groups and the rendered Loki rule keys;
            `rendered_loki_rule_keys` unless a test passes another.
        loki_rules_dir: The Loki ruler's rule files.
        render_cases: The alert groups and expected text of the notification renders.
    """
    try:
        found = (render or rendered_files)()
        groups, keys = (render_rules or rendered_rule_keys)()
        loki_groups, loki_keys = (render_loki_rules or rendered_loki_rule_keys)()
    except RuntimeError as err:
        # A Go expression outside a raw block usually lands here, as a Jinja syntax error.
        return [str(err)]
    problems = floor_problems(found) + rule_problems(
        groups,
        keys,
        rules_dir,
        tests_dir,
        prometheus_yml=found.get("prometheus.yml"),
    )
    problems += loki_group_problems(loki_groups, loki_keys, loki_rules_dir)
    if "alertmanager.yml" in found and "homelab.tmpl" in found:
        problems += go_template_problems(
            ALERTMANAGER_SOURCE.read_text(),
            found["alertmanager.yml"],
            found["homelab.tmpl"],
        )
    with tempfile.TemporaryDirectory(prefix="alert-rules-") as tmp:
        paths = {}
        for key, text in found.items():
            paths[key] = Path(tmp) / key
            paths[key].write_text(text)
        for argv in tool_checks(paths, rules_dir, tests_dir, loki_rules_dir):
            code, output = run(argv)
            if code != 0:
                problems.append(f"`{' '.join(argv)}` exited {code}:\n{output.rstrip()}")
    problems += render_problems(run, cases=render_cases)
    return problems


def prove_red(run: Runner = run_tool, fixtures: Path = FIXTURES) -> list[str]:
    """A problem for each tool that fails to accept its good fixture or reject its bad one."""
    problems = []
    for proof in PROOFS:
        command = " ".join((proof.tool, *proof.args))
        code, output = run([proof.tool, *proof.args, str(fixtures / proof.good)])
        if code != 0:
            problems.append(
                f"`{command}` exited {code} on {proof.good}, which it must accept. Until it "
                f"does, a rejection proves nothing:\n{output.rstrip()}"
            )
        code, output = run([proof.tool, *proof.args, str(fixtures / proof.bad)])
        if code == 0:
            problems.append(f"`{command}` accepted {proof.bad}, which it must reject")
        elif proof.marker not in output:
            problems.append(
                f"`{command}` exited {code} on {proof.bad} without saying "
                f"{proof.marker!r}:\n{output.rstrip()}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--prove-red",
        action="store_true",
        help="run each tool against its good and bad fixture instead of the tree",
    )
    parser.add_argument(
        "--write-renders",
        action="store_true",
        help="rewrite the expected notification renders from homelab.tmpl instead of checking",
    )
    args = parser.parse_args(argv)
    if args.write_renders:
        problems = render_problems(run_tool, write=True)
    elif args.prove_red:
        problems = prove_red()
    else:
        problems = check_tree()
    for problem in problems:
        print(f"[FAIL] {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
