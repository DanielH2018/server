"""The round trip: build each placeholder directory and its stub twin with `flux build`, then compare.

`shadow_diff.py roundtrip` renders both modes into one directory, each in C1's layout:
`DIR/placeholder/`, and a stub render per pass. For every directory under `cluster/` it writes
two throwaway Flux Kustomizations under `DIR/kustomizations/` that differ only in `postBuild`,
and runs `flux build kustomization <dir> --dry-run --kustomization-file <file> --path <dir>`
once per mode:

- **Placeholder side, a directory that substitutes.** The file carries every key's R1 stub
  value inline in `postBuild.substitute`, and the command adds `--strict-substitute`.
- **Placeholder side, a directory that does not substitute.** The file has no `postBuild`, so
  the comparison proves the renderer escaped nothing there.
- **Stub side.** The file has no `postBuild`, so nothing is substituted.

Which directories substitute comes from the render's own key sets
(`placeholders.PlaceholderRender.key_sets`), never from the output. `flux build` runs the
controller's own `SubstituteVariables` per resource and serialises both sides with the same
code, so the outputs compare byte for byte, and the report names the first object that
differs. It prints no file content: every value on either side is a stub.

It runs two passes, `stub_passes`. The first substitutes each key's R1 stub, a plain-safe
string. The second sets every key the standalone-value guard does not cover to
`TYPE_HOSTILE_STUB` on both sides, and keeps every other key's R1 stub. The pinned CLI drops
the template's quotes around a substituted value, so a value such as `0123` changes type where
it stands alone in a field. A key in the guard's map under any tag, or in `GUARD_DEFERRED`, is
covered, so a difference in the second pass marks a standalone use the renderer's detection
missed.

A role's `render_sops_manifests` files are in neither build. Placeholder mode emits no document
for them, and `render_sides` drops the same files from each stub render. Each pass counts the
documents it dropped there, and fails unless the count equals the number of entries in the
tags it built, so an entry that hides more than its one Secret cannot read as clean.

`roundtrip` needs the pinned flux CLI. Where `flux` is not on `PATH`, it refuses and never
reports a pass. `docs/homelab-render.md` is the operator's page.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.exit_codes import SHADOW_DIFFERENT, SHADOW_OK
from renderer.renderer_lib.cluster import (
    DEFERRED_SUFFIX,
    RenderedTag,
    render_tag,
    write_cluster,
)
from renderer.renderer_lib.placeholders import (
    GUARD_DEFERRED,
    PlaceholderRender,
    exclude_sops_manifests,
    render_placeholders,
    sops_manifests,
)

PLACEHOLDER, STUB = "placeholder", "stub"
KUSTOMIZATIONS = "kustomizations"
# One namespace for every throwaway Kustomization: `flux build` stamps the Kustomization's
# name and namespace on each object it builds, so the two sides must share both.
FLUX_NAMESPACE = "flux-system"
BUILD_TIMEOUT_SECONDS = 120

# The `run` callable `build` takes: the subprocess call, or a test's stand-in for it.
Runner = Callable[..., subprocess.CompletedProcess]


class FluxMissing(RuntimeError):
    """No `flux` on `PATH`, so no round trip can run, and none may report a pass."""


def find_flux() -> str:
    """The path of the `flux` binary on `PATH`.

    Raises:
        FluxMissing: There is none.
    """
    path = shutil.which("flux")
    if path is None:
        raise FluxMissing(
            "no flux CLI on PATH. The round trip runs in CI, which installs the pinned "
            "CLI; it reports no pass without one"
        )
    return path


def kustomization_file(name: str, substitute: Mapping[str, str] | None) -> str:
    """A throwaway Flux Kustomization `name`, with `postBuild.substitute` only when given."""
    spec: dict = {
        "interval": "10m",
        "path": "./",
        "prune": True,
        "sourceRef": {"kind": "GitRepository", "name": FLUX_NAMESPACE},
    }
    if substitute is not None:
        spec["postBuild"] = {"substitute": dict(sorted(substitute.items()))}
    return yaml.safe_dump(
        {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": name, "namespace": FLUX_NAMESPACE},
            "spec": spec,
        },
        sort_keys=False,
        width=4096,
    )


def build(
    flux: str, name: str, path: Path, kfile: Path, strict: bool, run: Runner
) -> subprocess.CompletedProcess:
    """Run `flux build kustomization` over `path` with the Kustomization in `kfile`."""
    command = [
        flux,
        "build",
        "kustomization",
        name,
        "--path",
        str(path),
        "--kustomization-file",
        str(kfile),
        "--dry-run",
    ]
    if strict:
        command.append("--strict-substitute")
    return run(
        command,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        check=False,
    )


def documents(output: str) -> list[str]:
    """The YAML documents of a `flux build` output, as text, empty ones dropped."""
    docs, current = [], []
    for line in output.splitlines(keepends=True):
        if line.rstrip("\n") == "---":
            docs.append("".join(current))
            current = []
        else:
            current.append(line)
    docs.append("".join(current))
    return [d for d in docs if d.strip()]


def object_name(doc: str) -> str:
    """`Kind/namespace/name` of one YAML document, for a report line."""
    try:
        obj = yaml_fast.safe_load(doc) or {}
    except yaml.YAMLError:
        return "an unparseable document"
    meta = obj.get("metadata") or {}
    return f"{obj.get('kind')}/{meta.get('namespace', '')}/{meta.get('name')}"


def first_difference(placeholder: str, stub: str) -> str | None:
    """The first object that differs between two `flux build` outputs, or None if equal."""
    if placeholder == stub:
        return None
    left, right = documents(placeholder), documents(stub)
    for a, b in zip(left, right, strict=False):
        if a != b:
            return f"{object_name(b)} differs"
    if len(left) != len(right):
        return f"{len(left)} objects on the placeholder side, {len(right)} on the stub side"
    return "the outputs differ outside any object"


# The value every key takes in the second pass. The pinned CLI substitutes into a resource it
# has already serialised, and it drops the template's quotes: measured on CI, a `stringData`
# value written `"${n}"` came back as the integer 83 for n=0123 and as a boolean for n=true
# (docs/homelab-render.md, *What the pinned flux CLI does*). An all-digit stub with a leading
# zero therefore shows every site where substitution would change a value's type.
TYPE_HOSTILE_STUB = "0123"


@dataclass(frozen=True)
class StubPass:
    """One stub value set a round trip compares the placeholder render against.

    Attributes:
        name: The directory under the round trip's root that holds this pass's stub render.
        label: How the summary line names the pass.
        values: The stub-mode render's overrides, as a stub file would hold them.
        substitute: Each secret key mapped to the value the placeholder side substitutes.
    """

    name: str
    label: str
    values: dict
    substitute: dict[str, str]


def guarded_keys(guard_map: Mapping[str, list[str]]) -> frozenset[str]:
    """The keys the standalone-value guard covers: mapped under any tag, or deferred."""
    return frozenset(k for keys in guard_map.values() for k in keys) | GUARD_DEFERRED


def stub_passes(
    stubs: dict, keys: list[str], guarded: frozenset[str]
) -> list[StubPass]:
    """The two passes: each key's R1 stub, then every key outside `guarded` at `TYPE_HOSTILE_STUB`.

    The second pass's stub side is a stub-mode render with those keys set to the hostile stub,
    so Jinja writes the template's quotes around it there. Flux does not on the placeholder
    side, so a value whose YAML type substitution changes shows as a difference. A key in
    `guarded` keeps its R1 stub, because the guard refuses a real value Flux would retype.
    """
    first = {k: str(stubs[k]) for k in keys}
    hostile = {k: TYPE_HOSTILE_STUB for k in keys if k not in guarded}
    return [
        StubPass(STUB, "each key's R1 stub", stubs, first),
        StubPass(
            f"{STUB}-{TYPE_HOSTILE_STUB}",
            f"every key outside the standalone guard {TYPE_HOSTILE_STUB}",
            {**stubs, **hostile},
            {**first, **hostile},
        ),
    ]


@dataclass
class Report:
    """The verdict of one pass of a round trip.

    Attributes:
        label: The pass's label, from `StubPass.label`.
        tags: The tags rendered on both sides.
        skipped: The requested tags placeholder mode skipped, from `PLACEHOLDER_DEFERRED`.
        directories: Every directory compared.
        substituting: The directories built with `postBuild`.
        objects: The objects the stub side built, summed over the directories.
        differences: One line per directory whose two builds differ or fail.
        sops_entries: The `render_sops_manifests` entries of the tags built.
        sops_documents: The documents the stub render dropped for those entries.
    """

    label: str
    tags: list[str]
    skipped: list[str]
    directories: list[str] = field(default_factory=list)
    substituting: list[str] = field(default_factory=list)
    objects: int = 0
    differences: list[str] = field(default_factory=list)
    sops_entries: int = 0
    sops_documents: int = 0

    def lines(self) -> list[str]:
        deferred = sum(d.endswith(DEFERRED_SUFFIX) for d in self.directories)
        lines = list(self.differences)
        if self.sops_documents != self.sops_entries:
            lines.append(
                f"roundtrip: dropped {self.sops_documents} documents for "
                f"{self.sops_entries} render_sops_manifests entries; each entry is one "
                "document"
            )
        if self.skipped:
            lines.append(
                f"roundtrip: skipped, in PLACEHOLDER_DEFERRED: {', '.join(self.skipped)}"
            )
        lines.append(
            f"roundtrip: {len(self.tags)} tags, {deferred} deferred directories, "
            f"{len(self.substituting)} substituting, {self.objects} objects, "
            f"{len(self.differences)} differences, {self.sops_documents} documents in "
            f"render_sops_manifests, with {self.label}"
        )
        return lines

    def exit_code(self) -> int:
        if self.differences or self.sops_documents != self.sops_entries:
            return SHADOW_DIFFERENT
        return SHADOW_OK


@dataclass(frozen=True)
class Sides:
    """The renders a round trip compares, written under one directory.

    Attributes:
        root: The directory every render and the throwaway Kustomizations live under.
        placeholder: The placeholder render, whose key sets decide `postBuild`.
        tags: The tags rendered in every mode.
        sops_entries: The `render_sops_manifests` entries of `tags`.
        sops_documents: Each pass's name mapped to the documents its stub render dropped
            for those entries.
    """

    root: Path
    placeholder: PlaceholderRender
    tags: list[str]
    sops_entries: int = 0
    sops_documents: dict[str, int] = field(default_factory=dict)

    def cluster(self, side: str) -> Path:
        return self.root / side / "cluster"


def render_sides(
    root: Path,
    tags: list[str],
    keys: list[str],
    passes: list[StubPass],
    stub_render: Callable[[str, dict], RenderedTag] = render_tag,
) -> Sides:
    """Render `tags` under `root`: `placeholder/`, and one stub render per pass.

    Each stub render drops the `render_sops_manifests` files, as placeholder mode does, and
    counts the documents it dropped. `stub_render` renders one tag in stub mode; a test
    passes one that changes the render.

    Raises:
        RenderError: A template fails to render.
        RenderRefused: A directory to write already holds files.
        ValueError: A pinned fact shares a name with a secret key.
    """
    placeholder = render_placeholders(tags, keys)
    rendered = [t.tag for t in placeholder.tags]
    entries = {tag: sops_manifests(tag) for tag in rendered}
    stub_renders: dict[str, list[RenderedTag]] = {}
    dropped_documents: dict[str, int] = {}
    for p in passes:
        renders, count = [], 0
        for tag in rendered:
            kept, dropped = exclude_sops_manifests(
                stub_render(tag, p.values), entries[tag]
            )
            renders.append(kept)
            count += sum(len(documents(text)) for text in dropped.values() if text)
        stub_renders[p.name] = renders
        dropped_documents[p.name] = count
    write_cluster(placeholder.tags, root / PLACEHOLDER)
    for name, renders in stub_renders.items():
        write_cluster(renders, root / name)
    total = sum(len(names) for names in entries.values())
    return Sides(root, placeholder, rendered, total, dropped_documents)


def compare_sides(
    sides: Sides,
    stub_pass: StubPass,
    flux: str,
    run: Runner = subprocess.run,
) -> Report:
    """Build every directory of the placeholder render and of one pass's stub render, and compare.

    Args:
        sides: The renders, from `render_sides`.
        stub_pass: The pass whose stub render and substitute values to use.
        flux: The flux binary.
        run: The subprocess call; a test passes a stand-in.
    """
    report = Report(
        stub_pass.label,
        sides.tags,
        sides.placeholder.skipped,
        sops_entries=sides.sops_entries,
        sops_documents=sides.sops_documents.get(stub_pass.name, 0),
    )
    kdir = sides.root / KUSTOMIZATIONS
    kdir.mkdir(parents=True, exist_ok=True)
    for name, keys in sorted(sides.placeholder.key_sets.items()):
        report.directories.append(name)
        if keys:
            report.substituting.append(name)
        outputs = {}
        for side in (PLACEHOLDER, stub_pass.name):
            substitutes = side == PLACEHOLDER and bool(keys)
            kfile = kdir / f"{name}.{stub_pass.name}.{side}.yaml"
            kfile.write_text(
                kustomization_file(name, stub_pass.substitute if substitutes else None)
            )
            path = sides.cluster(side) / name
            result = build(flux, name, path, kfile, substitutes, run)
            if result.returncode != 0:
                tail = " ".join(result.stderr.strip().splitlines()[-3:])
                report.differences.append(
                    f"{name}: flux build failed on the {side} side "
                    f"(exit {result.returncode}): {tail}"
                )
                break
            outputs[side] = result.stdout
        else:
            report.objects += len(documents(outputs[stub_pass.name]))
            difference = first_difference(outputs[PLACEHOLDER], outputs[stub_pass.name])
            if difference:
                report.differences.append(f"{name}: {difference}")
    return report
