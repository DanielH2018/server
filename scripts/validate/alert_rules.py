#!/usr/bin/env python3
"""Check the alert plane's inputs with the tools that load them: promtool and amtool.

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
- `promtool check rules` on every rule file in the role's `files/rules/`.
- `promtool test rules` on every test file in the role's `tests/rules/`.

The rendered `prometheus.yml` holds no stubbed value, so the stubbing changes nothing promtool
reads. `FLOOR` names the rendered files the run must find. A render that stopped producing
one fails instead of checking nothing.

THE TOOLS. `scripts/validate/alert_tools.sh` provisions both at the role's image tags into
`alert-tools/` under the git common dir, which every worktree of a clone shares, and every
tool call goes through it.

`--prove-red` runs each tool against `PROOFS`, the fixtures in
`scripts/validate/tests/fixtures/alert_rules/`. It exits 0 only when every tool accepts its
good fixture and rejects its bad one with the expected message. The accepting half proves the
tool ran at all: a missing binary also exits non-zero, and without the good fixture it would
read as a rejection. CI's `hooks` job runs this step after provisioning. The pytest shards
install no binaries, so that step is the tools' red proof.

Run directly or via the `validate-alert-rules` prek hook. Exits non-zero on any problem and
prints each one, with the tool's output, on stderr.
"""

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

ROLE = "observability"
ROLE_DIR = K8S_ROLES / ROLE
RULES_DIR = ROLE_DIR / "files" / "rules"
RULE_TESTS_DIR = ROLE_DIR / "tests" / "rules"
TOOLS = REPO / "scripts" / "validate" / "alert_tools.sh"
FIXTURES = REPO / "scripts" / "validate" / "tests" / "fixtures" / "alert_rules"

# Each rendered file the tool phase checks: the template that renders it, and the kind, name
# and data key of the object that carries it.
RENDERED = {
    "prometheus.yml": ("prometheus.yaml.j2", "ConfigMap", "prometheus-config"),
}

# The rendered files a run must find. The M1 slice plan raises it: alertmanager.yml joins it
# with the Alertmanager Secret.
FLOOR = frozenset({"prometheus.yml"})

# A tool run: the argv after `alert_tools.sh`, returning (exit code, combined output).
Runner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class Proof:
    """One tool's red proof: a fixture it must accept and one it must reject.

    Attributes:
        tool: `promtool` or `amtool`.
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


def tool_checks(
    rendered: Mapping[str, Path],
    rules_dir: Path = RULES_DIR,
    tests_dir: Path = RULE_TESTS_DIR,
) -> list[list[str]]:
    """The tool invocations for every discovered input, as `alert_tools.sh` argv lists."""
    checks = []
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
    return checks


def check_tree(run: Runner = run_tool) -> list[str]:
    """Render the role, apply the floor, and run every tool check over the tree."""
    found = rendered_files()
    problems = floor_problems(found)
    with tempfile.TemporaryDirectory(prefix="alert-rules-") as tmp:
        paths = {}
        for key, text in found.items():
            paths[key] = Path(tmp) / key
            paths[key].write_text(text)
        for argv in tool_checks(paths):
            code, output = run(argv)
            if code != 0:
                problems.append(f"`{' '.join(argv)}` exited {code}:\n{output.rstrip()}")
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
    args = parser.parse_args(argv)
    problems = prove_red() if args.prove_red else check_tree()
    for problem in problems:
        print(f"[FAIL] {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
