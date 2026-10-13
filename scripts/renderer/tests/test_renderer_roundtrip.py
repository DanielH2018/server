"""`shadow_diff.py roundtrip` and `fluxprobe`, driven through a fake `flux` on PATH.

The fake builds a directory by re-serialising its resources and, when the Kustomization
carries `postBuild.substitute`, by a minimal substitution of `$$` and `${name}`. It covers the
comparison, the reporting and the refusals, and proves nothing about Flux: CI's
`flux_roundtrip` job runs both subcommands against the pinned CLI.

Run: uv run pytest scripts/renderer/tests/test_renderer_roundtrip.py
"""

import sys

import pytest

from lib.proc_testing import fake_bin
from renderer import homelab_render, shadow_diff
from renderer.renderer_lib.roundtrip import compare_sides, render_sides, stub_passes

FAKE_FLUX = """#!{python}
import pathlib, re, sys, yaml
args = sys.argv[1:]
path = pathlib.Path(args[args.index("--path") + 1])
kfile = pathlib.Path(args[args.index("--kustomization-file") + 1])
values = (yaml.safe_load(kfile.read_text())["spec"].get("postBuild") or {{}}).get("substitute")
resources = yaml.safe_load((path / "kustomization.yaml").read_text())["resources"]
out = []
for resource in resources:
    for doc in yaml.safe_load_all((path / resource).read_text()):
        text = yaml.safe_dump(doc, sort_keys=True, width=4096)
        if values is not None:
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
    bin_dir = fake_bin(tmp_path / "bin", flux=FAKE_FLUX.format(python=sys.executable))
    monkeypatch.setenv("PATH", str(bin_dir))
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
    assert first.endswith(" 0 differences, with each key's R1 stub")
    assert second.endswith(" 0 differences, with every key 0123")


def test_a_one_byte_edit_to_a_secret_value_is_flagged(flux_on_path, tmp_path):
    keys = homelab_render.secret_keys()
    passes = stub_passes(shadow_diff.stub_vars(), keys)
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


def test_a_value_alone_in_a_quoted_field_differs_in_the_0123_pass(
    flux_on_path, tmp_path, capsys
):
    # ical-proxy's Secret holds `GOOGLE_ICS_URL: "{{ calendar_1 }}"`, a key alone in a quoted
    # field. Substituted, an all-digit value loses the template's quotes and changes type,
    # which the R1 stubs never show.
    out = tmp_path / "rt"
    assert _main("roundtrip", "--out", str(out), "--role", "ical-proxy") == 1
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].endswith(" 0 differences, with each key's R1 stub")
    assert printed[1].startswith("ical-proxy: ") and printed[1].endswith(" differs")
    assert printed[2].endswith(" 1 differences, with every key 0123")


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
    assert "FAIL probe-substitute red-proof-absent: missing from the output" in (
        capsys.readouterr().out
    )
