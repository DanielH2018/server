"""`shadow_diff.py roundtrip` and `fluxprobe`, driven through a fake `flux` on PATH.

The fake builds a directory by re-serialising its resources and, when the Kustomization
carries `postBuild.substitute`, by a minimal substitution of `$$` and `${name}`. For the flux
probe's resolver corpus it replays what the pinned CLI v2.9.6 returned, `MEASURED`. It covers
the comparison, the reporting and the refusals, and proves nothing about Flux: CI's
`flux_roundtrip` job runs both subcommands against the pinned CLI.

Run: uv run pytest scripts/renderer/tests/test_renderer_roundtrip.py
"""

import json
import re
import sys

import pytest

from lib.proc_testing import fake_bin
from renderer import homelab_render, shadow_diff
from renderer.renderer_lib.cluster import render_tag
from renderer.renderer_lib.flux_probe import RESOLVER_CORPUS, TYPES_FIXTURE_MANIFEST
from renderer.renderer_lib import roundtrip
from renderer.renderer_lib.placeholders import read_guard_map
from renderer.renderer_lib.secret_checks import KNOWN_VIOLATIONS
from renderer.renderer_lib.roundtrip import (
    compare_sides,
    guarded_keys,
    render_sides,
    stub_passes,
)

# The scalar the pinned CLI v2.9.6 printed for each resolver corpus value, built as a
# standalone `stringData` field; None where the build failed. Measured on daniel-server on
# 2026-10-13.
MEASURED = {
    "0123": "83",
    "true": "true",
    "yes": "true",
    "1e3": "1000",
    "null": "null",
    "0x1F": "31",
    "0o17": "15",
    "1:20": '"1:20"',
    "2001-12-14": '"2001-12-14T00:00:00Z"',
    "~": "null",
    "on": "true",
    ".inf": None,
    "": "null",
    "08": "8",
    "1.e3": "1000",
    "y": "true",
    "abc": "abc",
    "a.b-c.example": "a.b-c.example",
    "0123a": "0123a",
}

FAKE_FLUX = """#!{python}
import json, os, pathlib, re, sys, yaml
args = sys.argv[1:]
path = pathlib.Path(args[args.index("--path") + 1])
kfile = pathlib.Path(args[args.index("--kustomization-file") + 1])
values = (yaml.safe_load(kfile.read_text())["spec"].get("postBuild") or {{}}).get("substitute")
replay = json.loads(os.environ.get("FAKE_FLUX_REPLAY", "{{}}"))
if values and "probe_n" in values and values["probe_n"] in replay:
    scalar = replay[values["probe_n"]]
    if scalar is None:
        sys.exit("envsubst error: YAMLToJSON: json: unsupported value")
    print({manifest!r}.replace('"${{probe_n}}"', scalar), end="")
    sys.exit(0)
resources =yaml.safe_load((path / "kustomization.yaml").read_text())["resources"]
out = []
for resource in resources:
    for doc in yaml.safe_load_all((path / resource).read_text()):
        text = yaml.safe_dump(doc, sort_keys=True, width=4096)
        meta = doc.get("metadata") or {{}}
        opted_out = "disabled" in (
            (meta.get("labels") or {{}}).get("kustomize.toolkit.fluxcd.io/substitute"),
            (meta.get("annotations") or {{}}).get("kustomize.toolkit.fluxcd.io/substitute"),
        )
        if values is not None and not opted_out:
            def sub(m):
                if m.group(0) == "$$":
                    return "$"
                if m.group(1) not in values:
                    sys.exit(f"variable not set (strict mode): {{m.group(1)}}")
                return values[m.group(1)]
            text = yaml.safe_dump(
                yaml.safe_load(re.sub(r"\\$\\$|\\$\\{{(\\w+)\\}}", sub, text)),
                sort_keys=True,
                width=4096,
            )
        out.append(text)
print("---\\n".join(out), end="")
"""


@pytest.fixture
def flux_on_path(tmp_path, monkeypatch):
    fake = FAKE_FLUX.format(python=sys.executable, manifest=TYPES_FIXTURE_MANIFEST)
    bin_dir = fake_bin(tmp_path / "bin", flux=fake)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_FLUX_REPLAY", json.dumps(MEASURED))
    return str(bin_dir / "flux")


def _main(*args):
    return shadow_diff.main([*args])


def test_roundtrip_over_equal_renders_is_clean(flux_on_path, tmp_path, capsys):
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "configarr") == 0
    first, second = capsys.readouterr().out.splitlines()
    for summary in (first, second):
        assert summary.startswith(
            "roundtrip: 1 tags, 0 deferred directories, 1 substituting,"
        )
    assert first.endswith(
        " 0 differences, 0 documents in render_sops_manifests, with each key's R1 stub"
    )
    assert second.endswith(
        " 0 differences, 0 documents in render_sops_manifests, with every key outside the standalone guard 0123"
    )


def test_a_one_byte_edit_to_a_secret_value_is_flagged(flux_on_path, tmp_path):
    keys = homelab_render.secret_keys()
    guarded = guarded_keys(read_guard_map())
    passes = stub_passes(shadow_diff.stub_vars(), keys, guarded)
    sides = render_sides(tmp_path / "rt", ["configarr"], keys, passes)
    secret = sides.cluster("placeholder") / "configarr" / "config-secret.yaml"
    text = secret.read_text()
    assert "api_key: ${" in text
    secret.write_text(text.replace("api_key: ${", "api_kez: ${", 1))

    report = compare_sides(sides, passes[0], flux_on_path)
    assert report.exit_code() == 1
    assert report.differences == ["configarr: Secret/homelab/configarr-config differs"]


def test_a_substitute_value_changed_by_one_byte_is_flagged(
    flux_on_path, tmp_path, capsys
):
    out = tmp_path / "rt"
    assert (
        _main("roundtrip", "--out", str(out), "--role", "configarr", "--red-proof") == 1
    )
    printed = capsys.readouterr().out
    assert "changed by one byte" in printed
    assert "configarr: Secret/homelab/configarr-config differs" in printed


def test_a_standalone_use_the_guard_covers_is_clean_in_the_0123_pass(
    flux_on_path, tmp_path, capsys
):
    # traefik's Deployment holds `value: "{{ email }}"`, and the committed map guards
    # `email`, so the second pass keeps its R1 stub.
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "traefik") == 0
    assert capsys.readouterr().out.count(" 0 differences, ") == 2


def test_a_standalone_use_outside_the_map_is_flagged_in_the_0123_pass(
    flux_on_path, tmp_path, capsys
):
    # With `email` dropped from the map, `${email}` takes `0123`. Substituted, it loses the
    # template's quotes and changes type, which the R1 stubs never show. Every other key
    # keeps the committed map's cover, so the difference is `email`'s alone.
    without_email = {
        tag: [key for key in keys if key != "email"]
        for tag, keys in read_guard_map().items()
    }
    (tmp_path / "map.json").write_text(json.dumps(without_email))
    out = tmp_path / "rt"
    args = ("--role", "traefik", "--guard-map", str(tmp_path / "map.json"))
    assert _main("roundtrip", "--out", str(out), *args) == 1
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].endswith(
        " 0 differences, 0 documents in render_sops_manifests, with each key's R1 stub"
    )
    assert printed[1] == "traefik: Deployment/homelab/traefik differs"
    assert printed[2].endswith(
        " 1 differences, 0 documents in render_sops_manifests, with every key outside the standalone guard 0123"
    )


def test_observability_with_its_checksum_annotations_compared_is_clean(
    flux_on_path, tmp_path, capsys
):
    # #4952: Prometheus's `checksum/config` hashed a config holding `${domain}`, so the round
    # trip left the five checksum annotations out. With `domain` published, no hashed config
    # holds a placeholder, and the round trip compares every annotation.
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "observability") == 0
    assert capsys.readouterr().out.count(" 0 differences, ") == 2


def test_observability_with_one_checksum_annotation_changed_is_flagged(
    flux_on_path, tmp_path
):
    # The clean half above passes whether or not the annotations are compared, so this half
    # changes one hashed value on the placeholder side and requires the round trip to see it.
    keys = homelab_render.secret_keys()
    passes = stub_passes(shadow_diff.stub_vars(), keys, guarded_keys(read_guard_map()))
    sides = render_sides(tmp_path / "rt", ["observability"], keys, passes)
    annotation = re.compile(r"(checksum/config: )\S+")
    (path, *_) = [
        p
        for p in sorted(
            (sides.cluster(roundtrip.PLACEHOLDER) / "observability").glob("*.yaml")
        )
        if annotation.search(p.read_text())
    ]
    path.write_text(annotation.sub(r"\g<1>changed", path.read_text(), count=1))
    report = compare_sides(sides, passes[0], flux_on_path)
    assert report.exit_code() == 1
    (difference,) = report.differences
    assert difference.startswith("observability: ") and difference.endswith(" differs")


def test_the_flux_crds_that_opt_out_of_substitution_are_clean(
    flux_on_path, tmp_path, capsys
):
    # The CRDs carry `kustomize.toolkit.fluxcd.io/substitute: disabled`, and the Kustomization
    # CRD's description holds a literal `${`. An escape there would come back as `$${`.
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "flux") == 0
    assert capsys.readouterr().out.count(" 0 differences, ") == 2


def test_a_known_violation_document_left_out_is_clean(flux_on_path, tmp_path, capsys):
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "pi-peer-backup") == 0
    for summary in capsys.readouterr().out.splitlines():
        assert " 1 documents in KNOWN_VIOLATIONS left out, 0 differences" in summary


def _report(**fields) -> roundtrip.Report:
    return roundtrip.Report("a pass", [], [], **fields)


def test_exclusions_that_match_what_they_name_are_clean():
    report = _report(
        directories=["pi-peer-backup"],
        documents=["pi-peer-backup: Secret/pi-peer-backup-ssh"],
    )
    assert roundtrip.exclusion_problems(report, KNOWN_VIOLATIONS) == []


def test_exclusions_that_miss_what_they_name_are_flagged():
    report = _report(directories=["pi-peer-backup"])
    (problem,) = roundtrip.exclusion_problems(report, KNOWN_VIOLATIONS)
    assert "left out the documents [], where KNOWN_VIOLATIONS names" in problem


@pytest.mark.parametrize("extra", [[], ["--red-proof"]], ids=["green", "red-proof"])
def test_a_render_with_nothing_substituting_is_refused(
    flux_on_path, tmp_path, capsys, extra
):
    # registry renders no secret reference. Without this refusal the green run exited 0 with
    # `0 substituting`, and the red run's setup error exited 1, which CI's red half accepts.
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "registry", *extra) == 2
    captured = capsys.readouterr()
    assert "no rendered directory substitutes" in captured.err
    assert "roundtrip:" not in captured.out


def test_roundtrip_without_flux_on_path_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _main("roundtrip", "--out", str(tmp_path / "rt"), "--role", "configarr") == 2
    captured = capsys.readouterr()
    assert "reports no pass" in captured.err
    assert "roundtrip:" not in captured.out
    assert not (tmp_path / "rt").exists()


def test_fluxprobe_passes_every_case_the_fake_returns(flux_on_path, tmp_path, capsys):
    assert _main("fluxprobe", "--out", str(tmp_path / "probe")) == 0
    printed = capsys.readouterr().out
    assert "FAIL" not in printed
    assert (
        "ok probe-substitute escaped-run: '$$$$$${x}' came back as '$$${x}'" in printed
    )


def test_fluxprobe_with_an_expected_output_missing_is_flagged(
    flux_on_path, tmp_path, capsys
):
    assert _main("fluxprobe", "--out", str(tmp_path / "probe"), "--red-proof") == 1
    printed = capsys.readouterr().out
    assert "FAIL probe-substitute red-proof-absent: missing from the output" in printed
    # The red proof reads FLUX_KEEPS_BUT_REFUSED as empty, so the CLI's kept `1:20` fails.
    assert "FAIL resolver '1:20': " in printed


def test_fluxprobe_holds_the_resolver_to_the_measured_cli_is_clean(
    flux_on_path, tmp_path, capsys
):
    assert _main("fluxprobe", "--out", str(tmp_path / "probe")) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sum(ln.startswith("ok resolver ") for ln in lines) == len(RESOLVER_CORPUS)
    assert "ok resolver '1e3': the CLI returned 1000, the resolver refuses it" in lines


@pytest.mark.parametrize(
    ("value", "scalar", "failure"),
    [
        # A CLI that started typing `abc` would meet a resolver that passes it.
        ("abc", "null", "so the guard would pass a value Flux retypes"),
        # A CLI that kept `yes` would meet a resolver that refuses it.
        ("yes", '"yes"', "and it is not in FLUX_KEEPS_BUT_REFUSED"),
        # A CLI that started typing `1:20` leaves FLUX_KEEPS_BUT_REFUSED naming it.
        ("1:20", "80", "so FLUX_KEEPS_BUT_REFUSED is stale"),
    ],
)
def test_fluxprobe_where_the_resolver_and_the_cli_disagree_is_flagged(
    flux_on_path, tmp_path, monkeypatch, capsys, value, scalar, failure
):
    monkeypatch.setenv("FAKE_FLUX_REPLAY", json.dumps({**MEASURED, value: scalar}))
    assert _main("fluxprobe", "--out", str(tmp_path / "probe")) == 1
    failed = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("FAIL")
    ]
    assert len(failed) == 1, failed
    assert failed[0].startswith(f"FAIL resolver {value!r}: ")
    assert failure in failed[0]


def test_roundtrip_drops_one_document_per_sops_manifest_entry_is_clean(
    flux_on_path, tmp_path, capsys
):
    # home-assistant's render_sops_manifests names google-sa-secret.yaml, which placeholder
    # mode never emits, so the stub side drops it too and counts its one Secret.
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "home-assistant") == 0
    printed = capsys.readouterr().out
    assert printed.count(" 0 differences, 1 documents in render_sops_manifests, ") == 2
    assert not (
        out / "stub" / "cluster" / "home-assistant" / "google-sa-secret.yaml"
    ).exists()


def test_roundtrip_dropping_a_document_more_than_the_entries_is_flagged(
    flux_on_path, tmp_path
):
    def two_documents(tag, values):
        rendered = render_tag(tag, values)
        extra = rendered.files["google-sa-secret.yaml"]
        rendered.files["google-sa-secret.yaml"] += "---\n" + extra.replace(
            "home-assistant-google-sa", "home-assistant-hidden"
        )
        return rendered

    keys = homelab_render.secret_keys()
    passes = stub_passes(shadow_diff.stub_vars(), keys, guarded_keys(read_guard_map()))
    sides = render_sides(
        tmp_path / "rt", ["home-assistant"], keys, passes, two_documents
    )

    report = compare_sides(sides, passes[0], flux_on_path)
    assert report.differences == []
    assert report.exit_code() == 1
    assert (
        "roundtrip: dropped 2 documents for 1 render_sops_manifests entries; each entry "
        "is one document"
    ) in report.lines()
