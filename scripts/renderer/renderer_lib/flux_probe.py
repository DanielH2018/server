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
- a `stringData` value written `"${n}"`, built once with `n=0123` and once with `n=true`,
  comes back as the YAML type `EXPECTED_TYPES` records.

`--red-proof` adds one expected output that no fixture yields, so a working probe must fail.
The values are fixture strings, never a secret. `docs/homelab-render.md` records the measured
behaviours.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lib import yaml_fast

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

# The type each `n` value comes back as, measured against the pinned CLI on this branch's CI
# and recorded in docs/homelab-render.md. None measures without asserting.
EXPECTED_TYPES: dict[str, str | None] = {"0123": None, "true": None}


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
    'type: Opaque\nstringData:\n  n: "${n}"\n'
)


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
        result = _built(root, TYPES, {"n": value}, flux, run)
        if result.returncode != 0:
            report.fail(
                f"{TYPES} n={value}: flux build exited {result.returncode}: {result.stderr}"
            )
            continue
        line = next(
            (
                ln.strip()
                for ln in result.stdout.splitlines()
                if ln.strip().startswith("n:")
            ),
            "(no n line)",
        )
        got = type(_data(result.stdout, "Secret").get("n")).__name__
        measured = f"{TYPES} n={value}: comes back as {got}, from the line `{line}`"
        if expected is None:
            report.lines.append(f"measured {measured}")
        elif got != expected:
            report.fail(f"{measured}, not {expected}")
        else:
            report.ok(measured)
    return report
