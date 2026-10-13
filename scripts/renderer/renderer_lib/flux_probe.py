"""The fixture probe: what the pinned flux CLI does with the escapes placeholder mode writes.

`shadow_diff.py fluxprobe` writes four small fixture directories, builds each with
`flux build kustomization --dry-run`, and fails unless every expected output appears. The
round trip relies on each behaviour, so CI runs the probe before `roundtrip`, against the
same pinned binary, and a bump of the pin that changes one fails here first:

- `$${HOME}`, which pass 2 writes for a literal `${HOME}`, comes back as `${HOME}`;
- `$$$`, which pass 2 writes for a bare `$$`, comes back as `$$`;
- `$$$$$${x}`, which pass 2 writes for a literal `$$${x}`, comes back as `$$${x}`;
- `$$${K}`, which passes 2 and 3 write for a `$` directly before a secret reference, comes
  back as `$` plus the value;
- `${HOME}` in a directory with no `postBuild` comes back unchanged;
- a placeholder for a variable absent from `postBuild.substitute` fails the build under
  `--strict-substitute`;
- a `stringData` value written `"${probe_n}"`, built once with `probe_n=0123` and once with
  `probe_n=true`, comes back as the YAML type `EXPECTED_TYPES` records;
- the standalone-value guard's resolver, `scripts/lib/plain_scalar.py:refused`, refuses every
  `RESOLVER_CORPUS` value that the same field does not return as the identical string. It
  passes every other value but those in `FLUX_KEEPS_BUT_REFUSED`, which it refuses by design.

`--red-proof` adds one expected output that no fixture yields, and reads
`FLUX_KEEPS_BUT_REFUSED` as empty, so a working probe must fail twice.
The values are fixture strings, never a secret. `docs/homelab-render.md` records the measured
behaviours.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lib import plain_scalar, yaml_fast
from lib.exit_codes import SHADOW_DIFFERENT, SHADOW_OK
from renderer.renderer_lib.cluster import KUSTOMIZATION, kustomization_text
from renderer.renderer_lib.roundtrip import (
    Runner,
    build,
    documents,
    kustomization_file,
)

KEY, VALUE = "probe_key", "probe-value"
SUBSTITUTE, PLAIN, STRICT, TYPES = (
    "probe-substitute",
    "probe-plain",
    "probe-strict",
    "probe-types",
)

# Each ConfigMap key of the substituting fixture: the value as passes 2 and 3 write it, and
# the value Flux must return.
ESCAPE_CASES = {
    "escaped-brace": ("$${HOME}", "${HOME}"),
    "bare-double": ("$$$", "$$"),
    "escaped-run": ("$$$$$${x}", "$$${x}"),
    "dollar-before-reference": ("$$${" + KEY + "}", "$" + VALUE),
}
# The same for the fixture built with no `postBuild`.
PLAIN_CASES = {"literal-reference": ("${HOME}", "${HOME}")}
# The key `--red-proof` expects and no fixture holds.
RED_PROOF_KEY = "red-proof-absent"

# The type each `probe_n` value comes back as, measured against the pinned CLI v2.9.6 on CI
# run 38426501907 and recorded in docs/homelab-render.md. Flux substitutes into a resource it
# has already serialised without the template's quotes, so `0123` comes back as the YAML 1.1
# octal integer 83 and `true` as a boolean. The round trip's second pass exists because of
# this. A pin bump that keeps the quotes fails here, and that pass can then go. None measures
# without asserting.
EXPECTED_TYPES: dict[str, str | None] = {"0123": "int", "true": "bool"}

# The values the probe builds as a standalone `stringData` field to hold the guard's resolver
# to the pinned CLI. The five values the plan measured, the integer, null and timestamp forms
# around them, `08`, `1.e3` and `y`, which only the go-yaml port types, and three strings the
# CLI keeps. Measured against v2.9.6 on daniel-server on 2026-10-13: `2001-12-14` comes back
# as the string `2001-12-14T00:00:00Z`, so a value counts as kept only when it comes back as
# the identical string, and `.inf` fails the build.
RESOLVER_CORPUS = (
    "0123",
    "true",
    "yes",
    "1e3",
    "null",
    "0x1F",
    "0o17",
    "1:20",
    "2001-12-14",
    "~",
    "on",
    ".inf",
    "",
    "08",
    "1.e3",
    "y",
    "abc",
    "a.b-c.example",
    "0123a",
)
# The corpus values the CLI keeps and the resolver still refuses. PyYAML's YAML 1.1 reader
# types a sexagesimal number such as `1:20` as the integer 80, where go-yaml v2 reads a
# string. The resolver refuses on the union of the two readers, so it fails closed here.
FLUX_KEEPS_BUT_REFUSED = frozenset({"1:20"})


def _configmap(name: str, data: dict[str, str]) -> str:
    # Written by hand, double-quoted, as a template writes them: no value holds a quote or a
    # backslash, so each scalar is exactly the string between its quotes.
    lines = [f'  {key}: "{value}"' for key, value in data.items()]
    return (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        f"  name: {name}\n  namespace: probe\ndata:\n" + "\n".join(lines) + "\n"
    )


TYPES_SECRET = (
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: probe-types\n  namespace: probe\n"
    'type: Opaque\nstringData:\n  probe_n: "${probe_n}"\n'
)
# Not the plan's bare `n`: Go's YAML 1.1 reader takes the unquoted key `n` in
# `postBuild.substitute` for the boolean false, so the build found no variable `n` at all.
TYPE_VAR = "probe_n"


def write_fixtures(root: Path) -> None:
    """Write the four fixture directories under `root`, each with its `kustomization.yaml`."""
    fixtures = {
        SUBSTITUTE: _configmap(
            SUBSTITUTE, {k: v for k, (v, _) in ESCAPE_CASES.items()}
        ),
        PLAIN: _configmap(PLAIN, {k: v for k, (v, _) in PLAIN_CASES.items()}),
        STRICT: _configmap(STRICT, {"undefined": "${probe_undefined}"}),
        TYPES: TYPES_SECRET,
    }
    for name, text in fixtures.items():
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "resource.yaml").write_text(text)
        (directory / KUSTOMIZATION).write_text(kustomization_text(["resource.yaml"]))


@dataclass
class ProbeReport:
    """The probe's findings: one line per case, and the cases that failed."""

    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def ok(self, line: str) -> None:
        self.lines.append(f"ok {line}")

    def fail(self, line: str) -> None:
        self.lines.append(f"FAIL {line}")
        self.failures.append(line)

    def exit_code(self) -> int:
        return SHADOW_DIFFERENT if self.failures else SHADOW_OK


def _built(
    root: Path, name: str, substitute: dict[str, str] | None, flux: str, run: Runner
) -> subprocess.CompletedProcess:
    kfile = root / f"{name}.kustomization.yaml"
    kfile.write_text(kustomization_file(name, substitute))
    return build(flux, name, root / name, kfile, substitute is not None, run)


def _data(output: str, kind: str) -> dict:
    """The `data` or `stringData` of the one object of `kind` in a build output."""
    for doc in documents(output):
        obj = yaml_fast.safe_load(doc) or {}
        if obj.get("kind") == kind:
            return obj.get("data") or obj.get("stringData") or {}
    return {}


def _check_values(
    report: ProbeReport, name: str, result, cases: dict[str, tuple[str, str]]
) -> None:
    if result.returncode != 0:
        report.fail(f"{name}: flux build exited {result.returncode}: {result.stderr}")
        return
    data = _data(result.stdout, "ConfigMap")
    for key, (written, expected) in cases.items():
        if key not in data:
            report.fail(f"{name} {key}: missing from the output")
        elif data[key] != expected:
            report.fail(
                f"{name} {key}: {written!r} came back as {data[key]!r}, not {expected!r}"
            )
        else:
            report.ok(f"{name} {key}: {written!r} came back as {expected!r}")


def probe(
    root: Path, flux: str, run: Runner = subprocess.run, red_proof=False
) -> ProbeReport:
    """Write the fixtures under `root`, build each with `flux`, and check every output.

    Args:
        root: An empty directory to write the fixtures into.
        flux: The flux binary.
        run: The subprocess call; a test passes a stand-in.
        red_proof: Also expect one output no fixture yields, so the probe must fail.
    """
    write_fixtures(root)
    report = ProbeReport()
    cases = dict(ESCAPE_CASES)
    if red_proof:
        cases[RED_PROOF_KEY] = ("(nothing)", "(nothing)")
    _check_values(
        report, SUBSTITUTE, _built(root, SUBSTITUTE, {KEY: VALUE}, flux, run), cases
    )
    _check_values(report, PLAIN, _built(root, PLAIN, None, flux, run), PLAIN_CASES)

    strict = _built(root, STRICT, {KEY: VALUE}, flux, run)
    if strict.returncode == 0:
        report.fail(f"{STRICT}: an undefined variable built under --strict-substitute")
    else:
        report.ok(
            f"{STRICT}: an undefined variable failed the build (exit {strict.returncode})"
        )

    for value, expected in EXPECTED_TYPES.items():
        result = _built(root, TYPES, {TYPE_VAR: value}, flux, run)
        if result.returncode != 0:
            report.fail(
                f"{TYPES} {TYPE_VAR}={value}: flux build exited {result.returncode}: {result.stderr}"
            )
            continue
        line = next(
            (
                ln.strip()
                for ln in result.stdout.splitlines()
                if ln.strip().startswith(f"{TYPE_VAR}:")
            ),
            "(no such line)",
        )
        got = type(_data(result.stdout, "Secret").get(TYPE_VAR)).__name__
        measured = (
            f"{TYPES} {TYPE_VAR}={value}: comes back as {got}, from the line `{line}`"
        )
        if expected is None:
            report.lines.append(f"measured {measured}")
        elif got != expected:
            report.fail(f"{measured}, not {expected}")
        else:
            report.ok(measured)

    keeps_but_refused = frozenset() if red_proof else FLUX_KEEPS_BUT_REFUSED
    for value in RESOLVER_CORPUS:
        result = _built(root, TYPES, {TYPE_VAR: value}, flux, run)
        got = (
            _data(result.stdout, "Secret").get(TYPE_VAR, "(missing)")
            if result.returncode == 0
            else f"(build exited {result.returncode})"
        )
        kept = isinstance(got, str) and got == value
        refused = plain_scalar.refused(value)
        line = (
            f"resolver {value!r}: the CLI returned {got!r}, the resolver "
            f"{'refuses' if refused else 'passes'} it"
        )
        if not kept and not refused:
            report.fail(f"{line}, so the guard would pass a value Flux retypes")
        elif kept and refused and value not in keeps_but_refused:
            report.fail(f"{line}, and it is not in FLUX_KEEPS_BUT_REFUSED")
        elif not kept and value in keeps_but_refused:
            report.fail(
                f"{line}; the CLI no longer keeps it, so FLUX_KEEPS_BUT_REFUSED is stale"
            )
        else:
            report.ok(line)
    return report
